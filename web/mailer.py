"""Sending a report by email, over plain SMTP.

The app has no mail provider of its own: it authenticates to the sender's own
mailbox (SMTP_USER / SMTP_PASSWORD — for a Google Workspace account that's an
app password), so the report arrives *from* that person rather than from a
no-reply robot. Nothing is queued or retried: a send either works while the
request is open or it reports why it didn't.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage

_XLSX_TYPE = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
MAX_RECIPIENTS = 10


class NotConfigured(RuntimeError):
    """SMTP credentials are missing — the caller should say which ones."""


def default_recipients() -> list[str]:
    """Who a report goes to unless the sender edits the field."""
    return _split(os.environ.get("REPORT_TO", "zarco@znlove.xyz,angie@znlove.xyz"))


def sender() -> str:
    return os.environ.get("REPORT_FROM") or os.environ.get("SMTP_USER", "")


def configured() -> bool:
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def missing_vars() -> list[str]:
    return [v for v in ("SMTP_USER", "SMTP_PASSWORD") if not os.environ.get(v)]


def _split(raw: str) -> list[str]:
    return [a.strip() for a in re.split(r"[,;\s]+", raw or "") if a.strip()]


def clean_recipients(raw: str | list[str]) -> list[str]:
    """Validated, de-duplicated recipient list. Raises ValueError on junk so a
    typo'd address fails loudly instead of silently dropping a recipient."""
    addrs = _split(raw) if isinstance(raw, str) else [a.strip() for a in raw if a.strip()]
    seen, out = set(), []
    for a in addrs:
        if not _EMAIL_RE.match(a):
            raise ValueError(f"{a} is not an email address")
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    if not out:
        raise ValueError("no recipients")
    if len(out) > MAX_RECIPIENTS:
        raise ValueError(f"more than {MAX_RECIPIENTS} recipients")
    return out


def send_report(to: list[str], subject: str, body: str,
                attachment: bytes, filename: str) -> dict:
    """Send one message with the workbook attached. Returns {"ok", "to", "from"}."""
    if not configured():
        raise NotConfigured(", ".join(missing_vars()))
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_addr = sender()

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(attachment, maintype=_XLSX_TYPE[0], subtype=_XLSX_TYPE[1],
                       filename=filename)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    return {"ok": True, "to": to, "from": from_addr}
