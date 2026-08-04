"""One Google authorization, shared by the Gmail and Sheets calls.

Both features ride on the same refresh token (minted by
src/google_oauth_setup.py), so the app asks for both scopes at once: sending
mail as the user, and creating the spreadsheets it makes. `drive.file` is the
narrow one — it grants access only to files this app created, never to the rest
of the Drive.

Config: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN.
"""
from __future__ import annotations

import os
import time

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",
)

_VARS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")

# access tokens last an hour; keep one rather than paying a round trip per call
_token: dict = {"value": None, "expires": 0.0}


class GoogleError(RuntimeError):
    """Google said no — carries the message Google actually returned."""


def configured() -> bool:
    return all(os.environ.get(v) for v in _VARS)


def missing_vars() -> list[str]:
    return [v for v in _VARS if not os.environ.get(v)]


def reason(res: httpx.Response) -> str:
    """Google's own words for a failure, short enough for a status bar."""
    try:
        data = res.json()
    except ValueError:
        return f"HTTP {res.status_code}"
    err = data.get("error")
    if isinstance(err, dict):
        detail = err.get("message") or err.get("status") or ""
    else:  # the token endpoint returns {"error": …, "error_description": …}
        detail = " — ".join(x for x in (err, data.get("error_description")) if x)
    return f"HTTP {res.status_code}: {detail}".strip(": ").strip()


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
        raise GoogleError(f"could not reach Google to refresh the token: {e}") from e
    if res.status_code != 200:
        raise GoogleError(f"refreshing the Google token failed — {reason(res)}")
    payload = res.json()
    _token["value"] = payload["access_token"]
    # renew a minute early, so a token can't expire mid-request
    _token["expires"] = now + max(60, int(payload.get("expires_in", 3600)) - 60)
    return _token["value"]


def call(method: str, url: str, **kw) -> httpx.Response:
    """An authorized request, with one forced token refresh on a 401."""
    for attempt in (1, 2):
        token = access_token(force=attempt == 2)
        headers = dict(kw.pop("headers", {}), Authorization=f"Bearer {token}")
        try:
            res = httpx.request(method, url, timeout=60, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise GoogleError(f"could not reach Google: {e}") from e
        if res.status_code == 401 and attempt == 1:
            continue
        if res.status_code >= 300:
            raise GoogleError(reason(res))
        return res
    raise GoogleError("Google kept rejecting the token")  # unreachable in practice
