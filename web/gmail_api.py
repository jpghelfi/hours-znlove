"""Sending through the Gmail API over HTTPS.

Why not SMTP: Render's free instances block outbound traffic to ports 25, 465
and 587, so *any* SMTP send from the deployed app fails with "Network is
unreachable" — a different password, or SMTP with OAuth (XOAUTH2), would fail
the same way, since the block is on the port, not the credentials. The Gmail
API is ordinary HTTPS on 443, which is not blocked.

It also sidesteps app passwords entirely (this Workspace has them disabled) and
files the message in the sender's real Sent folder, because Gmail itself is
doing the sending.

Authorization lives in google_auth.py, shared with the Sheets export — one
refresh token, minted by src/google_oauth_setup.py.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage

from .google_auth import GoogleError, access_token, call, configured, missing_vars  # noqa: F401

SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

GmailError = GoogleError  # the name mailer.explain() and its tests already use


def send(msg: EmailMessage) -> None:
    """Hand a built message to Gmail. Raises GoogleError with Google's reason."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    call("POST", SEND_URL, json={"raw": raw})
