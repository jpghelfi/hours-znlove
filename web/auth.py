"""Notion OAuth login, gated by the People-db roster (with an env fallback).

Flow: /login -> Notion consent -> /auth/callback -> exchange code -> read the
authorizing user's identity -> check it against the roster. Access is curated
in Notion: an Active People row grants login, an Admin tick grants team-wide
reports (matched by the linked Notion user id). ALLOWED_EMAILS / ADMIN_EMAILS
remain as a fallback so a People-db misconfig can't lock everyone out. OAuth is
used only to authenticate the person; all Notion data access still uses the
integration token (NOTION_TOKEN).
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from urllib.parse import urlencode, urlparse

import httpx

from . import notion_ops as ops

AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"


def auth_disabled() -> bool:
    """Local-dev bypass. NEVER set AUTH_DISABLED=1 in production."""
    return os.environ.get("AUTH_DISABLED") == "1"


def allowed_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}


def _admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_allowed(user: dict | None) -> bool:
    """May this person log in? Primary source is the People db (any Active row,
    matched by the linked Notion user id); ALLOWED_EMAILS stays a fallback so a
    People-db misconfig can't lock everyone out."""
    if not user:
        return False
    uid, email = user.get("id"), user.get("email")
    if uid and uid in ops.access_ids()["allowed"]:
        return True
    return bool(email) and email.strip().lower() in allowed_emails()


def is_admin(user: dict | None) -> bool:
    """May this person see team-wide reports and exports? Admins are the People
    db rows ticked Admin (matched by Notion user id); ADMIN_EMAILS is a fallback."""
    if not user:
        return False
    uid, email = user.get("id"), user.get("email")
    if uid and uid in ops.access_ids()["admins"]:
        return True
    return bool(email) and email.strip().lower() in _admin_emails()


def _cfg(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing env var {key} (needed for Notion OAuth login).")
    return val


def _auth_params(state: str) -> dict:
    return {
        "client_id": _cfg("NOTION_OAUTH_CLIENT_ID"),
        "response_type": "code",
        "owner": "user",
        "redirect_uri": _cfg("NOTION_OAUTH_REDIRECT_URI"),
        "state": state,
    }


def login_url(state: str) -> str:
    """The documented authorize endpoint. It answers with a 302 to Notion's
    real consent page — see consent_url for why we'd rather not use that 302."""
    return f"{AUTHORIZE_URL}?{urlencode(_auth_params(state))}"


# ---- the consent page, reached without a redirect ----------------------
#
# api.notion.com/v1/oauth/authorize is a redirector: it 302s to Notion's actual
# consent page, today https://app.notion.com/install-integration. That extra hop
# is what broke sign-in on iOS. The Notion app claims app.notion.com as a
# universal link, and although Notion explicitly *excludes* /install-integration
# from that claim, the exclusion is only reliably honoured when iOS evaluates
# the URL the user actually tapped. Arriving there through a server redirect,
# Safari hands the navigation to the Notion app instead — which has no idea what
# to do with an OAuth consent request, so it opens to nothing and the flow dies.
#
# So: ask Notion once where its consent page lives, cache that, and point the
# sign-in button straight at it with the same query Notion would have built. The
# tapped URL is then the excluded path, with no redirect for iOS to mis-handle.
# Nothing is hardcoded — if Notion moves the page, the next probe follows it —
# and any failure falls back to the documented endpoint, i.e. exactly today's
# behaviour.

_CONSENT_TTL = 3600.0
_consent_cache: dict = {"at": 0.0, "base": None}
_consent_lock = threading.Lock()

# Only Notion may host Notion's consent page. Without this an open redirect on
# api.notion.com would become one on our sign-in button.
_NOTION_HOSTS = ("notion.com", "notion.so")


def _probe_consent_base() -> str | None:
    """Follow one hop of the authorize redirect and keep its scheme/host/path."""
    try:
        resp = httpx.get(login_url("probe"), follow_redirects=False, timeout=8)
    except Exception:
        logging.exception("Could not reach Notion to resolve its consent page.")
        return None
    loc = resp.headers.get("location") if resp.is_redirect else None
    if not loc:
        return None
    parsed = urlparse(loc)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == h or host.endswith("." + h)
                                           for h in _NOTION_HOSTS):
        logging.warning("Notion's authorize endpoint redirected somewhere unexpected (%s); "
                        "using the authorize URL directly.", loc)
        return None
    return f"https://{parsed.netloc}{parsed.path}"


def consent_url(state: str) -> str:
    """Where to send the browser to ask the person for consent.

    Notion's consent page directly when we can resolve it, so the sign-in
    button is a one-hop navigation (see the note above — this is what keeps iOS
    Safari from handing the tap to the Notion app). The documented authorize
    endpoint otherwise, which is what this always used to do.
    """
    now = time.monotonic()
    with _consent_lock:
        base, at = _consent_cache["base"], _consent_cache["at"]
    if base is None or now - at >= _CONSENT_TTL:
        base = _probe_consent_base()
        with _consent_lock:
            # a failed probe is cached too, so a Notion outage doesn't put an
            # 8-second timeout in front of every render of the login page
            _consent_cache.update(at=now, base=base or "")
    if not base:
        return login_url(state)
    return f"{base}?{urlencode(_auth_params(state))}"


def exchange_code(code: str) -> dict:
    """Exchange an auth code for a token; return the authorizing user {id,name,email}."""
    client_id = _cfg("NOTION_OAUTH_CLIENT_ID")
    client_secret = _cfg("NOTION_OAUTH_CLIENT_SECRET")
    redirect_uri = _cfg("NOTION_OAUTH_REDIRECT_URI")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    resp = httpx.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"},
        json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    owner = data.get("owner", {})
    user = owner.get("user", {}) if owner.get("type") == "user" else {}
    user_id = user.get("id")
    if not user_id:
        raise RuntimeError("Notion OAuth response had no user identity.")

    # Resolve full profile (email) via the integration token — reliable source of truth.
    profile = ops.get_user(user_id)
    return profile
