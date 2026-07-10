"""Notion OAuth login, gated by an email allowlist.

Flow: /login -> Notion consent -> /auth/callback -> exchange code -> read the
authorizing user's identity -> check email against ALLOWED_EMAILS. OAuth is used
only to authenticate the person; all Notion data access still uses the
integration token (NOTION_TOKEN).
"""
from __future__ import annotations

import base64
import os
from urllib.parse import urlencode

import httpx

from . import notion_ops as ops

AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"


def auth_disabled() -> bool:
    """Local-dev bypass. NEVER set AUTH_DISABLED=1 in production."""
    return os.environ.get("AUTH_DISABLED") == "1"


def allowed_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}


def is_allowed(email: str | None) -> bool:
    if not email:
        return False
    return email.strip().lower() in allowed_emails()


def _cfg(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing env var {key} (needed for Notion OAuth login).")
    return val


def login_url(state: str) -> str:
    params = {
        "client_id": _cfg("NOTION_OAUTH_CLIENT_ID"),
        "response_type": "code",
        "owner": "user",
        "redirect_uri": _cfg("NOTION_OAUTH_REDIRECT_URI"),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


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
