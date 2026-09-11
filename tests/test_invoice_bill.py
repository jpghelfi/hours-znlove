#!/usr/bin/env python3
"""Tests for what the bill says: its lines, hours and amount.

Run:  ./.venv/bin/python tests/test_invoice_bill.py

Plain asserts and a tiny runner, same shape as the other suites here. Nothing
touches Notion or Google: the PDF is built in memory from a dict, the line
parser and the prefills are pure functions, and the email body is checked
through app.py's builder with a hand-made invoice.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-for-anything")
os.environ["INVOICE_TAX_PCT"] = "21"
os.environ["INVOICE_TAX_LABEL"] = "VAT"

from reportlab import rl_config  # noqa: E402

# Uncompressed content streams, so the text a page draws is readable in the
# raw bytes — the assertions below grep the PDF rather than depend on a parser.
rl_config.pageCompression = 0

from web import invoice_pdf  # noqa: E402
from web.notion_ops import bill_lines, MAX_BILL_LINES, MAX_LINE_CHARS  # noqa: E402

_FAILS: list[str] = []


def check(name):
    def deco(fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception:
            _FAILS.append(name)
            print(f"  FAIL {name}\n{traceback.format_exc()}")
    return deco


INVOICE = {
    "id": "inv-1", "project_id": "p1", "project": "Kepos", "month": "2026-08-01",
    "period_label": "August 2026", "number": "2026-014", "issued": "2026-09-01",
    "hours_tracked": 45, "hours_billed": 42, "rate": 100.0, "currency": "USD",
    "amount": None, "lines": [], "client_note": "", "adjustments": {},
}


def pdf_text(data: bytes) -> str:
    """Enough of the PDF to assert on: with compression off, reportlab writes
    Helvetica text as literal strings inside the content streams, so the raw
    bytes contain every word drawn (escaped parentheses aside)."""
    return data.decode("latin-1").replace("\\(", "(").replace("\\)", ")")


# ---- the line parser ----------------------------------------------------

@check("bill_lines: one per line, blanks and surrounding whitespace dropped")
def _():
    assert bill_lines("  Website maintenance \n\n\r\n Landing page  build \n") == [
        "Website maintenance", "Landing page build"]


@check("bill_lines: capped in count and in length")
def _():
    many = "\n".join(f"line {i}" for i in range(MAX_BILL_LINES + 5))
    assert len(bill_lines(many)) == MAX_BILL_LINES
    assert len(bill_lines("x" * (MAX_LINE_CHARS * 2))[0]) == MAX_LINE_CHARS


@check("bill_lines: empty in, empty out")
def _():
    assert bill_lines("") == []
    assert bill_lines(None) == []


# ---- the prefills ---------------------------------------------------------

@check("default_lines names the project and the month")
def _():
    assert invoice_pdf.default_lines(INVOICE) == ["Kepos — August 2026"]
    assert invoice_pdf.default_lines({}) == ["Services"]


@check("bill_amount: hours × rate until an amount is saved, then the amount — including 0")
def _():
    assert invoice_pdf.bill_amount(INVOICE) == 4200.0
    assert invoice_pdf.bill_amount(dict(INVOICE, amount=3000)) == 3000.0
    assert invoice_pdf.bill_amount(dict(INVOICE, amount=0)) == 0.0
    assert invoice_pdf.bill_amount(dict(INVOICE, rate=None)) == 0.0


@check("totals: tax follows the typed amount, not hours × rate")
def _():
    s = invoice_pdf.totals(42, 100, 3000)
    assert s["subtotal"] == 3000 and s["tax"] == 630 and s["total"] == 3630
    s = invoice_pdf.totals(42, 100)
    assert s["subtotal"] == 4200 and s["total"] == 5082
    # an amount typed for a project with no rate is still taxed
    s = invoice_pdf.totals(42, 0, 1000)
    assert s["tax"] == 210
    assert invoice_pdf.totals(42, 0)["total"] == 0


# ---- the document ---------------------------------------------------------

@check("PDF: carries the lines and the totals, not the entries")
def _():
    doc = dict(INVOICE, lines=["Website maintenance", "Landing page build"], amount=3000)
    text = pdf_text(invoice_pdf.build(doc, {"name": "Kepos Inc"}))
    assert "Website maintenance" in text and "Landing page build" in text
    assert "3,000.00" in text and "3,630.00" in text
    assert "Detail" not in text and "Every entry" not in text


@check("PDF: the rate is printed only while the amount is still hours × rate")
def _():
    text = pdf_text(invoice_pdf.build(dict(INVOICE, lines=["x"])))
    assert "Rate" in text and "100.00/h" in text
    text = pdf_text(invoice_pdf.build(dict(INVOICE, lines=["x"], amount=3000)))
    assert "/h" not in text


@check("PDF: no rate and no amount is a statement of hours")
def _():
    text = pdf_text(invoice_pdf.build(dict(INVOICE, rate=None, lines=["x"])))
    assert "Total hours" in text and "Subtotal" not in text and "$" not in text


@check("PDF: no lines falls back to the default line")
def _():
    text = pdf_text(invoice_pdf.build(dict(INVOICE, lines=[])))
    assert "August 2026" in text


# ---- the covering note ----------------------------------------------------

@check("email body lists the lines and the total, and no longer promises a second page")
def _():
    from web.app import _invoice_email_body
    body = _invoice_email_body(dict(INVOICE, lines=["Website maintenance", "Landing page"],
                                    amount=3000), "August 2026", {"name": "JP"})
    assert "- Website maintenance\n- Landing page" in body
    assert "42 h — $3,630.00 (incl. VAT)" in body
    assert "second page" not in body
    assert "invoice 2026-014" in body


@check("email body for a bill with no money says the hours only")
def _():
    from web.app import _invoice_email_body
    body = _invoice_email_body(dict(INVOICE, rate=None), "August 2026", {"email": "a@b"})
    assert "Total: 42 h." in body
    assert "- Kepos — August 2026" in body


if _FAILS:
    print(f"\n{len(_FAILS)} failed: {', '.join(_FAILS)}")
    sys.exit(1)
print("\nall good")
