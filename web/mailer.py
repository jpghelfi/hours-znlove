"""Sending a report by email, as the sender rather than as a robot.

Two transports, picked in this order:

1. **Gmail API over HTTPS** (`gmail_api.py`) — what production uses. Render's
   free instances block outbound SMTP ports, so an SMTP send from the deployed
   app can never connect; the Gmail API is plain HTTPS. It also needs no app
   password (this Workspace has them disabled) and files the message in the
   sender's Sent folder.
2. **SMTP** — kept for a paid instance or a local run, where ports are open.

Either way the message goes out from a person's own mailbox. Nothing is queued
or retried: a send either works while the request is open or it reports why not.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage

from . import gmail_api

_XLSX_TYPE = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
MAX_RECIPIENTS = 10


class NotConfigured(RuntimeError):
    """No transport is configured — the caller should say what's missing."""


def default_recipients() -> list[str]:
    """Who a report goes to unless the sender edits the field."""
    return _split(os.environ.get("REPORT_TO", "zarco@znlove.xyz,angie@znlove.xyz"))


def enabled() -> bool:
    """Whether the email feature is switched on at all (REPORT_EMAIL_ENABLED).

    Off by default, and deliberately separate from having credentials: the
    Google authorization exists for the Sheets export too, so configuring that
    must not make an email UI reappear on the export screen uninvited.
    """
    return os.environ.get("REPORT_EMAIL_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def budget_alerts_enabled() -> bool:
    """Whether budget threshold emails are switched on (BUDGET_ALERTS_ENABLED).

    Its own switch, for the same reason enabled() is separate from having
    credentials: the one Google authorization powers the Sheets export and the
    report email, and turning either of those on must not silently start
    emailing people every time a project nears its budget.
    """
    return os.environ.get("BUDGET_ALERTS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def budget_recipients() -> list[str]:
    """Who a budget alert goes to. Falls back to the report recipients."""
    raw = os.environ.get("BUDGET_ALERT_TO", "").strip()
    return _split(raw) if raw else default_recipients()


def _transport_for(switch: bool) -> str:
    if not switch:
        return ""
    if gmail_api.configured():
        return "gmail"
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        return "smtp"
    return ""


def transport() -> str:
    """Which transport a report send would use: "gmail", "smtp", or "" for none."""
    return _transport_for(enabled())


def budget_transport() -> str:
    """Same, for budget alerts, which ride their own switch."""
    return _transport_for(budget_alerts_enabled())


def sender() -> str:
    """The From address. Gmail fills it in from the authorized account when
    REPORT_FROM isn't set, so an empty string here is not an error."""
    return os.environ.get("REPORT_FROM") or os.environ.get("SMTP_USER", "")


def configured() -> bool:
    return bool(transport())


def missing_vars() -> list[str]:
    """What to set to make a send possible — the Gmail path, since that's the
    one that works on Render, plus the switch that turns the feature on."""
    return ([] if enabled() else ["REPORT_EMAIL_ENABLED=1"]) + gmail_api.missing_vars()


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


def explain(exc: BaseException) -> str:
    """A short, admin-facing reason a send failed.

    Gmail and SMTP both say something useful when they refuse (a Google error
    message, 535 for bad credentials, 550 for a refused relay); without it the
    screen can only say "rejected", which is no help when the fix is a
    different credential — or a port that was never open in the first place.
    """
    if isinstance(exc, gmail_api.GmailError):
        return " ".join(str(exc).split())[:300]
    if isinstance(exc, OSError) and exc.errno in (101, 111, 110):
        return ("the mail port is blocked from this server — Render's free instances "
                "can't open SMTP connections, so send through the Gmail API instead")
    refused = getattr(exc, "recipients", None)
    if refused:  # SMTPRecipientsRefused: name the address the server bounced
        addr, (rcode, rmsg) = next(iter(refused.items()))
        if isinstance(rmsg, bytes):
            rmsg = rmsg.decode("utf-8", "replace")
        return " ".join(f"{rcode} {rmsg} ({addr})".split())[:300]
    code = getattr(exc, "smtp_code", None)
    raw = getattr(exc, "smtp_error", None)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    detail = f"{code} {raw}".strip() if code else (str(exc).strip() or type(exc).__name__)
    detail = " ".join(detail.split())[:300]
    if code == 535 or "Username and Password not accepted" in detail:
        detail += " — SMTP_PASSWORD has to be a Google app password, not the account password"
    return detail


def build_message(to: list[str], subject: str, body: str,
                  attachment: bytes, filename: str, from_addr: str = "") -> EmailMessage:
    msg = EmailMessage()
    if from_addr:  # Gmail fills this in itself when it isn't set
        msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(attachment, maintype=_XLSX_TYPE[0], subtype=_XLSX_TYPE[1],
                       filename=filename)
    return msg


def _deliver(msg: EmailMessage, via: str, from_addr: str) -> dict:
    """Hand one built message to the chosen transport."""
    if via == "gmail":
        gmail_api.send(msg)
        return {"ok": True, "to": msg["To"], "from": from_addr or "your Google account",
                "via": "gmail"}

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    return {"ok": True, "to": msg["To"], "from": from_addr, "via": "smtp"}


def send_report(to: list[str], subject: str, body: str,
                attachment: bytes, filename: str) -> dict:
    """Send one message with the workbook attached, over whichever transport is
    configured. Returns {"ok", "to", "from", "via"}."""
    via = transport()
    if not via:
        raise NotConfigured(", ".join(missing_vars()))
    from_addr = sender()
    msg = build_message(to, subject, body, attachment, filename, from_addr)
    out = _deliver(msg, via, from_addr)
    out["to"] = to
    return out


def send_plain(to: list[str], subject: str, body: str) -> dict:
    """Send a body-only message — no attachment — over the budget-alert switch.

    send_report requires a workbook; a budget alert is three lines of text.
    Same transports, same credentials, different switch: BUDGET_ALERTS_ENABLED
    rather than REPORT_EMAIL_ENABLED.
    """
    via = budget_transport()
    if not via:
        raise NotConfigured(
            ", ".join(([] if budget_alerts_enabled() else ["BUDGET_ALERTS_ENABLED=1"])
                      + gmail_api.missing_vars()))
    from_addr = sender()
    msg = EmailMessage()
    if from_addr:
        msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    out = _deliver(msg, via, from_addr)
    out["to"] = to
    return out
