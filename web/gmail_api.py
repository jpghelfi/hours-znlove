"""Sending through the Gmail API over HTTPS.

Why not SMTP: Render's free instances block outbound traffic to ports 25, 465
and 587, so *any* SMTP send from the deployed app fails with "Network is
unreachable" — a different password, or SMTP with OAuth (XOAUTH2), would fail
the same way, since the block is on the port, not the credentials. The Gmail
API is ordinary HTTPS on 443, which is not blocked.

It also sidesteps app passwords entirely (this Workspace has them disabled) and
files the message in the sender's real Sent folder, because Gmail itself is
doing the sending.

Configuration (see src/gmail_oauth_setup.py, which mints the refresh token):
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN.
"""
from __future__ import annotations

import base64
import os
import time
from email.message import EmailMessage

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPE = "https://www.googleapis.com/auth/gmail.send"

_VARS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")

# access tokens last an hour; keep one rather than paying a round trip per send
_token: dict = {"value": None, "expires": 0.0}


class GmailError(RuntimeError):
    """Google said no — carries the message Google actually returned."""


def configured() -> bool:
    return all(os.environ.get(v) for v in _VARS)


def missing_vars() -> list[str]:
    return [v for v in _VARS if not os.environ.get(v)]


def access_token(force: bool = False) -> str:
    """A live access token, refreshed from the stored refresh token."""
    now = time.monotonic()
    if not force and _token["value"] and now < _token["expires"]:
        return _token["value"]
    try:
        res = httpx.post(TOKEN_URL, timeout=30, data={
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        })
    except httpx.HTTPError as e:
        raise GmailError(f"could not reach Google to refresh the token: {e}") from e
    if res.status_code != 200:
        raise GmailError(f"refreshing the Google token failed — {_reason(res)}")
    payload = res.json()
    _token["value"] = payload["access_token"]
    # renew a minute early, so a token can't expire mid-request
    _token["expires"] = now + max(60, int(payload.get("expires_in", 3600)) - 60)
    return _token["value"]


def _reason(res: httpx.Response) -> str:
    """Google's own words for a failure, short enough for a status bar."""
    try:
        data = res.json()
    except ValueError:
        return f"HTTP {res.status_code}"
    err = data.get("error")
    if isinstance(err, dict):
        detail = err.get("message") or err.get("status") or ""
    else:  # the token endpoint returns {"error": "...", "error_description": "..."}
        detail = " — ".join(x for x in (err, data.get("error_description")) if x)
    return f"HTTP {res.status_code}: {detail}".strip(": ").strip()


def send(msg: EmailMessage) -> None:
    """Hand a built message to Gmail. Raises GmailError with Google's reason."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    for attempt in (1, 2):  # a stale cached token gets exactly one retry
        token = access_token(force=attempt == 2)
        try:
            res = httpx.post(SEND_URL, timeout=60, json={"raw": raw},
                             headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise GmailError(f"could not reach Gmail: {e}") from e
        if res.status_code < 300:
            return
        if res.status_code == 401 and attempt == 1:
            continue
        raise GmailError(_reason(res))
