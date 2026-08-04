#!/usr/bin/env python3
"""One-time: authorize the app to send mail and create sheets as you.

Run this on your own machine — it opens a browser for consent and catches the
redirect on localhost. Nothing is stored: it prints the three values to paste
into Render, and the refresh token is the only long-lived secret.

    ./.venv/bin/python src/google_oauth_setup.py

Before running, in console.cloud.google.com (as jp.ghelfi@znlove.xyz):
  1. Create a project (any name).
  2. APIs & Services → Library → enable **Gmail API** *and* **Google Sheets API**.
  3. OAuth consent screen → **Internal** (Workspace only — this matters: an
     External app in "testing" hands out refresh tokens that die after 7 days).
  4. Credentials → Create credentials → OAuth client ID → **Desktop app**.
  5. Copy the client ID and client secret, and have them ready here.
"""
from __future__ import annotations

import http.server
import json
import os
import secrets
import socketserver
import sys
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = " ".join((
    "https://www.googleapis.com/auth/gmail.send",   # send the report
    "https://www.googleapis.com/auth/drive.file",   # create the sheet it makes
))
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"

_result: dict = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _result.update({k: v[0] for k, v in q.items()})
        ok = "code" in _result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<h2>All set — close this tab and go back to the terminal.</h2>" if ok
            else b"<h2>No code came back. Check the terminal.</h2>")

    def log_message(self, *args):  # keep the console clean
        pass


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        sys.exit(f"\nGoogle refused the exchange: {e.read().decode()[:500]}")


def main() -> None:
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or input("Client ID: ").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or input("Client secret: ").strip()
    if not client_id or not client_secret:
        sys.exit("Both the client ID and the client secret are required.")

    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",     # ask for a refresh token
        "prompt": "consent",          # and force one even on a repeat run
        "state": state,
    })
    print("\nOpening the consent screen. Sign in as the account the reports should "
          "come FROM\nand own the sheets.\nIf no browser opens, paste this URL yourself:\n\n" + url + "\n")
    webbrowser.open(url)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("localhost", PORT), _Handler) as httpd:
        httpd.handle_request()  # one request: the redirect back from Google

    if _result.get("state") != state:
        sys.exit("The state didn't match — start over.")
    if "code" not in _result:
        sys.exit(f"No code came back: {_result.get('error', 'unknown error')}")

    tokens = _post(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _result["code"],
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    })
    refresh = tokens.get("refresh_token")
    if not refresh:
        sys.exit("Google returned no refresh token. Re-run — the consent screen has "
                 "to be accepted fresh (prompt=consent), not silently reused.")

    print("\nDone. Set these three in Render → hours-znlove → Environment:\n")
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={refresh}")
    print("\n(Optional) REPORT_FROM=your.name@znlove.xyz to pin the From address.")


if __name__ == "__main__":
    main()
