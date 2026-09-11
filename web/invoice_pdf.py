"""The invoice as a PDF — the document that actually goes to a client.

The other two exports answer a different question. `/project.csv` and the
workbook are *hours*: rows a PM reads. This is a **bill**: a numbered document
with a company on it, an addressee, a rate, an amount and a total, laid out so
it can be forwarded to a client's accounts payable without editing.

Kept out of app.py (routes) and notion_ops.py (Notion reads/writes) for the
same reason report_xlsx.py is: this module only turns an invoice that has
already been read into bytes.

**The bill says what the sender typed, not what was logged.** It used to list
one line per person and, on a second page, every entry with its comment. A
client doesn't need the log — that's what the workbook is for — and a bill
that copies it is a bill nobody wants to edit. So the document carries the
invoice's *lines* (one or more lines of detail, written on the invoice page
before it goes out), its total hours and its amount, all prefilled from the
month and all editable before sending. `default_lines()` and `bill_amount()`
are those prefills, shared with the screen so the two can't drift.

**Money is optional.** A project with no `Rate` still produces a valid
document — the rate and amount columns simply aren't drawn, and it reads as a
statement of hours. That's deliberate: the Rate lives in Notion and nobody is
going to fill it in for all 35 projects the day this ships, and an invoice
showing "0.00" would be worse than one showing none.

Everything about the sender (company name, address, tax id, payment
instructions, tax rate) is environment configuration, not code — see
`company()`.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#1B2138")
MUTED = colors.HexColor("#6B7280")
RULE = colors.HexColor("#D6DBE6")
HEAD_BG = colors.HexColor("#EEF2F9")

# Symbols for the currencies we actually bill in; anything else is printed as
# its code ("CHF 1,200.00"), which is unambiguous and never wrong.
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "ARS": "$"}

_MARGIN = 16 * mm


def default_currency() -> str:
    return (os.environ.get("INVOICE_CURRENCY") or "USD").strip().upper()[:8] or "USD"


def money(amount: float, currency: str = "") -> str:
    cur = (currency or default_currency()).upper()
    sym = _SYMBOLS.get(cur)
    body = f"{amount:,.2f}"
    return f"{sym}{body}" if sym else f"{cur} {body}"


def _num(raw, fallback: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _lines(raw: str) -> list[str]:
    """A multi-line env var. Render's dashboard makes real newlines awkward, so
    a pipe works as a line break too."""
    parts: list[str] = []
    for chunk in (raw or "").replace("\\n", "\n").split("\n"):
        parts.extend(p.strip() for p in chunk.split("|"))
    return [p for p in parts if p]


def company() -> dict:
    """Who is sending the bill. All environment, so a different entity (or a
    changed tax id) is a dashboard edit rather than a deploy."""
    return {
        "name": os.environ.get("INVOICE_COMPANY", "ZN Love").strip() or "ZN Love",
        "details": _lines(os.environ.get("INVOICE_COMPANY_DETAILS", "")),
        "payment": _lines(os.environ.get("INVOICE_PAYMENT", "")),
        "footer": os.environ.get("INVOICE_FOOTER", "").strip(),
        "tax_label": os.environ.get("INVOICE_TAX_LABEL", "VAT").strip() or "VAT",
        "tax_pct": _num(os.environ.get("INVOICE_TAX_PCT"), 0.0),
        "due_days": int(_num(os.environ.get("INVOICE_DUE_DAYS"), 0)),
    }


def tax_pct() -> float:
    return company()["tax_pct"]


def totals(hours: float, rate: float, subtotal: float | None = None) -> dict:
    """Subtotal / tax / total for a bill — shared with the screens, so what the
    browser previews and what the PDF prints can't drift.

    `subtotal` overrides the hours × rate arithmetic with the amount the bill
    actually says — the sender can type over it before sending, so a
    discounted or capped month is the amount on the bill, not the product of
    two numbers the client never sees.
    """
    co = company()
    if subtotal is None:
        subtotal = round(float(hours) * float(rate or 0), 2)
    subtotal = round(float(subtotal), 2)
    tax = round(subtotal * co["tax_pct"] / 100, 2) if subtotal else 0.0
    return {"subtotal": subtotal, "tax": tax, "tax_pct": co["tax_pct"],
            "tax_label": co["tax_label"], "total": round(subtotal + tax, 2)}


def default_lines(invoice: dict) -> list[str]:
    """What the bill says when nobody has typed anything: the project and the
    month. One line, so the sender has something to edit rather than a blank."""
    project = (invoice.get("project") or "").strip()
    period = (invoice.get("period_label") or "").strip()
    return [" — ".join(p for p in (project, period) if p) or "Services"]


def bill_amount(invoice: dict) -> float:
    """The pre-tax amount the bill says. `amount` on the row once one has been
    saved, hours × rate until then — a row filed before amounts existed still
    prices itself."""
    amount = invoice.get("amount")
    if amount is not None:
        return round(float(amount), 2)
    return round(float(invoice.get("hours_billed") or 0) * float(invoice.get("rate") or 0), 2)


def _styles() -> dict:
    base = getSampleStyleSheet()["BodyText"]

    def s(name, **kw):
        return ParagraphStyle(name, parent=base, **kw)

    return {
        "title": s("t", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=INK),
        "h": s("h", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=INK,
               spaceAfter=4),
        "label": s("l", fontName="Helvetica-Bold", fontSize=7.5, leading=10, textColor=MUTED),
        "body": s("b", fontSize=9, leading=12, textColor=INK),
        "muted": s("m", fontSize=8.5, leading=11.5, textColor=MUTED),
        "cell": s("c", fontSize=8.5, leading=11),
        "cellr": s("cr", fontSize=8.5, leading=11, alignment=TA_RIGHT),
        "right": s("r", fontSize=9, leading=12, textColor=INK, alignment=TA_RIGHT),
        "big": s("g", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK,
                 alignment=TA_RIGHT),
    }


def _esc(text) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _para_block(lines: list[str], style) -> Paragraph:
    return Paragraph("<br/>".join(_esc(line) for line in lines if line), style)


def _hours(value: float) -> str:
    return f"{round(float(value or 0), 2):g}"


def _human_date(iso: str) -> str:
    try:
        return dt.date.fromisoformat(str(iso)[:10]).strftime("%d %b %Y")
    except (ValueError, TypeError):
        return iso or ""


def _meta_table(invoice: dict, co: dict, st: dict, width: float) -> Table:
    """Number / issued / due / period, right-aligned beside the company name."""
    issued = invoice.get("issued") or ""
    due = ""
    if co["due_days"] and issued:
        try:
            due = (dt.date.fromisoformat(issued[:10])
                   + dt.timedelta(days=co["due_days"])).isoformat()
        except ValueError:
            due = ""
    pairs = [("Invoice", invoice.get("number") or "—"),
             ("Issued", _human_date(issued)),
             ("Period", invoice.get("period_label") or "")]
    if due:
        pairs.append(("Due", _human_date(due)))
    data = [[Paragraph(k.upper(), st["label"]), Paragraph(_esc(v), st["right"])]
            for k, v in pairs]
    t = Table(data, colWidths=[width * 0.4, width * 0.6])
    t.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _items_table(lines: list[str], hours: float, amount: float, currency: str,
                 st: dict, width: float, billable: bool) -> Table:
    """The bill's one line item: the detail lines as its description, the
    total hours beside them and the amount when there is money on the bill.

    One row, not one per line: the lines are what the work *was*, and the
    hours and amount are the sender's totals — splitting either across lines
    would mean inventing numbers nobody typed.
    """
    if billable:
        head = ["Description", "Hours", "Amount"]
        widths = [width - 75 * mm, 25 * mm, 50 * mm]
    else:
        head = ["Description", "Hours"]
        widths = [width - 30 * mm, 30 * mm]

    data = [[Paragraph(f"<b>{h}</b>", st["cell"] if i == 0 else st["cellr"])
             for i, h in enumerate(head)]]
    desc = "<br/>".join(_esc(line) for line in lines) or "&nbsp;"
    row = [Paragraph(desc, st["cell"]), Paragraph(_hours(hours), st["cellr"])]
    if billable:
        row.append(Paragraph(money(amount, currency), st["cellr"]))
    data.append(row)

    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _totals_table(hours: float, rate: float, amount: float, currency: str, st: dict,
                  width: float, billable: bool) -> Table:
    sums = totals(hours, rate, amount)
    rows = []
    if billable:
        rows.append(("Total hours", _hours(hours) + " h"))
        # the rate is printed only when the amount is still hours × rate — a
        # typed-over amount is the bill, and a rate beside it that doesn't
        # multiply into it is exactly what accounts payable bounces
        if rate and abs(round(hours * rate, 2) - sums["subtotal"]) < 0.005:
            rows.append(("Rate", money(rate, currency) + "/h"))
        rows.append(("Subtotal", money(sums["subtotal"], currency)))
        if sums["tax_pct"]:
            rows.append((f"{sums['tax_label']} {sums['tax_pct']:g}%",
                         money(sums["tax"], currency)))
    data = [[Paragraph(k, st["muted"]), Paragraph(_esc(v), st["right"])] for k, v in rows]
    label = "Total" if billable else "Total hours"
    value = money(sums["total"], currency) if billable else _hours(hours) + " h"
    data.append([Paragraph(f"<b>{label}</b>", st["body"]), Paragraph(_esc(value), st["big"])])

    t = Table(data, colWidths=[width * 0.55, width * 0.45], hAlign="RIGHT")
    t.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.6, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def filename(invoice: dict) -> str:
    """A filename a client can file: the number when there is one, the project
    and month otherwise."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", invoice.get("project") or "invoice").strip("-").lower()
    tail = re.sub(r"[^A-Za-z0-9-]+", "-",
                  str(invoice.get("number") or invoice.get("month") or "")[:20]).strip("-")
    return f"invoice-{slug}-{tail}.pdf" if tail else f"invoice-{slug}.pdf"


def build(invoice: dict, client: dict | None = None) -> bytes:
    """The invoice PDF.

    `invoice` carries project, number, month, period_label, issued, rate,
    currency, `hours_billed`, `amount`, `lines` and an optional client-facing
    note. The lines, hours and amount are what the sender confirmed on the
    invoice page (`default_lines` / `bill_amount` stand in until then), so the
    file a client is emailed says exactly what was on screen — and nothing
    about who logged what on which day.
    """
    co = company()
    st = _styles()
    client = client or {}
    rate = float(invoice.get("rate") or 0)
    currency = (invoice.get("currency") or default_currency()).upper()
    hours = round(float(invoice.get("hours_billed") or 0), 2)
    amount = bill_amount(invoice)
    lines = [l for l in (invoice.get("lines") or []) if l] or default_lines(invoice)
    # money columns are drawn when there is money on the bill: a rate on the
    # project, or an amount typed in by hand for a project that has none
    billable = bool(rate or amount)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=_MARGIN,
        title=f"Invoice {invoice.get('number') or ''} — "
              f"{invoice.get('project') or ''}".strip(),
        author=co["name"], subject=invoice.get("period_label") or "")
    width = doc.width
    story: list = []

    # ---- header: who is billing, and the invoice's own identity
    left = [Paragraph(_esc(co["name"]), st["title"])]
    if co["details"]:
        left += [Spacer(1, 3), _para_block(co["details"], st["muted"])]
    head = Table([[left, _meta_table(invoice, co, st, width * 0.38)]],
                 colWidths=[width * 0.62, width * 0.38])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 10 * mm)]

    # ---- who it's for, and what it covers
    bill_to = [client.get("name") or invoice.get("project") or ""]
    bill_to += _lines(client.get("address") or "")
    if client.get("email"):
        bill_to.append(client["email"])
    for_block = [Paragraph("PROJECT", st["label"]),
                 Paragraph(_esc(invoice.get("project") or ""), st["body"]),
                 Spacer(1, 4),
                 Paragraph("PERIOD", st["label"]),
                 Paragraph(_esc(invoice.get("period_label") or ""), st["body"])]
    who = Table([[[Paragraph("BILL TO", st["label"]), Spacer(1, 2),
                   _para_block(bill_to, st["body"])], for_block]],
                colWidths=[width * 0.55, width * 0.45])
    who.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [who, Spacer(1, 8 * mm)]

    # ---- the bill itself
    story += [_items_table(lines, hours, amount, currency, st, width, billable),
              Spacer(1, 6 * mm),
              _totals_table(hours, rate, amount, currency, st, width * 0.45, billable)]

    if invoice.get("client_note"):
        story += [Spacer(1, 8 * mm), Paragraph("NOTES", st["label"]),
                  Paragraph(_esc(invoice["client_note"]), st["body"])]
    if co["payment"]:
        story += [Spacer(1, 8 * mm), Paragraph("PAYMENT", st["label"]),
                  _para_block(co["payment"], st["body"])]
    if co["footer"]:
        story += [Spacer(1, 6 * mm), Paragraph(_esc(co["footer"]), st["muted"])]

    doc.build(story)
    return buf.getvalue()
