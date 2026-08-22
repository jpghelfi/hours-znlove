"""Harvest REST v2 — transport only, no Notion.

Split out of sync_harvest.py so the CLI and the web app share one client. The
web adapter (web/harvest_ops.py) turns what comes back into Notion rows; this
module knows nothing about them.

Credentials are two env vars, HARVEST_ACCOUNT_ID and HARVEST_TOKEN (a personal
access token from https://id.getharvest.com/developers). **A token inherits the
role of whoever minted it**, which matters more here than it looks: a `member`
token can read every time entry in the account (`timers:read:all`) but 403s on
/v2/users and sees only its own projects on /v2/projects. That is why nothing
here fetches users or projects — everything the sync needs travels embedded on
each time entry (`user`, `project`, `client`), which is readable either way.
"""
from __future__ import annotations

import os

import httpx

API = "https://api.harvestapp.com/v2"
_TIMEOUT = 30.0
PER_PAGE = 2000          # Harvest's documented maximum; July's 3,043 entries = 2 calls
_MAX_PAGES = 40          # a runaway-pagination backstop, far above any real range


def account_id() -> str:
    return (os.environ.get("HARVEST_ACCOUNT_ID") or "").strip()


def token() -> str:
    return (os.environ.get("HARVEST_TOKEN") or "").strip()


def enabled() -> bool:
    """Both credentials present. Everything Harvest-shaped degrades on this
    rather than erroring, the way invoices_enabled()/ticket_create_enabled() do."""
    return bool(account_id() and token())


def _headers() -> dict:
    return {
        "Harvest-Account-Id": account_id(),
        "Authorization": "Bearer " + token(),
        "User-Agent": "hours-znlove sync",
    }


def time_entries(date_from: str, date_to: str, client: httpx.Client | None = None) -> list[dict]:
    """Every time entry with a spent_date in [date_from, date_to], all users.

    Harvest pages with an explicit `next_page` rather than an offset, so the
    loop follows that. The date filter is Harvest's own — the caller still
    re-checks each entry's spent_date, because a paging mistake should drop
    rows out of the plan rather than silently write them onto the wrong week.
    """
    if not enabled():
        raise RuntimeError("HARVEST_ACCOUNT_ID and HARVEST_TOKEN are not set")
    own = client is None
    client = client or httpx.Client(timeout=_TIMEOUT)
    out: list[dict] = []
    try:
        page = 1
        for _ in range(_MAX_PAGES):
            res = client.get(API + "/time_entries", headers=_headers(), params={
                "from": date_from, "to": date_to, "per_page": PER_PAGE, "page": page,
            })
            res.raise_for_status()
            data = res.json()
            out.extend(data.get("time_entries") or [])
            if not data.get("next_page"):
                break
            page = data["next_page"]
    finally:
        if own:
            client.close()
    return out


def explain(exc: Exception) -> str:
    """Harvest's error payload as one sentence — the mailer.explain() pattern.

    A 401 here almost always means the token was revoked or belongs to another
    account, and a 403 means the token's role can't see what was asked for; say
    so, because the raw body is just {"error":"..."} or a bare status line.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = ""
        try:
            body = exc.response.json()
            detail = body.get("error_description") or body.get("error") or body.get("message") or ""
        except Exception:
            detail = (exc.response.text or "").strip()[:200]
        if code == 401:
            return "Harvest rejected the credentials — check HARVEST_ACCOUNT_ID and HARVEST_TOKEN."
        if code == 403:
            return ("Harvest refused that read for this token's role. "
                    + (detail or "The token may not have access to the account's time entries."))
        if code == 429:
            return "Harvest is rate-limiting us — wait a moment and try again."
        return f"Harvest returned {code}." + (" " + detail if detail else "")
    if isinstance(exc, httpx.RequestError):
        return "Could not reach Harvest — check the connection."
    return str(exc) or "Harvest request failed."
