#!/usr/bin/env python3
"""Tests for the invoice lifecycle: Draft → Approved → Sent → Paid (and Void).

Run:  ./.venv/bin/python tests/test_invoice_flow.py

Plain asserts and a tiny runner, same shape as the other suites here. Nothing
touches Notion: the client is swapped for a fake that serves one page and
records what would have been written.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-for-anything")

from web import auth  # noqa: E402
from web import notion_ops as ops  # noqa: E402
from web import app as webapp  # noqa: E402

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


INV_DS = "inv-ds"


def page(status=None, sent_at=None, history="", due=None):
    props = {
        "Project": {"relation": [{"id": "p1"}]},
        "Month": {"date": {"start": "2026-09-01"}},
        "Hours tracked": {"number": 40}, "Hours billed": {"number": 38},
        ops.HISTORY_PROP: {"rich_text": [{"plain_text": history}] if history else []},
    }
    if status:
        props[ops.STATUS_PROP] = {"select": {"name": status}}
    if sent_at:
        props[ops.SENT_AT_PROP] = {"date": {"start": sent_at}}
    if due:
        props[ops.DUE_PROP] = {"date": {"start": due}}
    return {"id": "inv1", "parent": {"data_source_id": INV_DS}, "properties": props}


class fake_notion:
    """Serve one invoice page; record every update."""

    def __init__(self, pg):
        self.pg, self.updates = pg, []

    def __enter__(self):
        outer = self

        class _N:
            class pages:
                @staticmethod
                def retrieve(pid):
                    return outer.pg

                @staticmethod
                def update(pid, **kw):
                    outer.updates.append(kw)
                    return outer.pg

        self._saved = (ops._notion, ops._person_name_map, ops._project_name_map, ops.INVOICES_DS)
        ops._notion = _N
        ops._person_name_map = lambda: {}
        ops._project_name_map = lambda: {"p1": "Kepos"}
        ops.INVOICES_DS = INV_DS
        return self

    def __exit__(self, *exc):
        ops._notion, ops._person_name_map, ops._project_name_map, ops.INVOICES_DS = self._saved

    @property
    def props(self):
        return self.updates[-1]["properties"]


def refused(fn, exc):
    try:
        fn()
    except exc as e:
        return str(e)
    raise AssertionError(f"expected {exc.__name__}")


# ---- who may move what -------------------------------------------------

@check("an admin approves; the accountant can't")
def _():
    assert ops.can_move_invoice("Draft", "Approved", admin=True, accountant=True) is None
    assert ops.can_move_invoice("Draft", "Approved", admin=False, accountant=True) == \
        "only an admin can do that"


@check("the accountant sends and marks paid; a plain user can't")
def _():
    for frm, to in (("Approved", "Sent"), ("Sent", "Paid"), ("Paid", "Sent"), ("Sent", "Approved")):
        assert ops.can_move_invoice(frm, to, admin=False, accountant=True) is None, (frm, to)
        assert ops.can_move_invoice(frm, to, admin=False, accountant=False), (frm, to)


@check("an admin can make every move the accountant can")
def _():
    for (frm, to) in ops.INVOICE_FLOW:
        assert ops.can_move_invoice(frm, to, admin=True, accountant=False) is None, (frm, to)


@check("no skipping: a draft can't be marked paid, a paid one can't be voided")
def _():
    assert ops.can_move_invoice("Draft", "Paid", admin=True, accountant=True)
    assert ops.can_move_invoice("Draft", "Sent", admin=True, accountant=True)
    assert ops.can_move_invoice("Paid", "Void", admin=True, accountant=True)
    assert ops.can_move_invoice("Sent", "Bogus", admin=True, accountant=True)
    assert ops.can_move_invoice("Sent", "Sent", admin=True, accountant=True)


@check("an accountant is matched by People-db id, an admin passes too")
def _():
    old = ops.access_ids
    ops.access_ids = lambda: {"allowed": set(), "admins": {"a1"}, "approvers": set(),
                              "accountants": {"c1"}}
    try:
        assert auth.is_accountant({"id": "c1"}) and not auth.is_admin({"id": "c1"})
        assert auth.is_accountant({"id": "a1"})
        assert not auth.is_accountant({"id": "u9", "email": "x@example.com"})
        assert not auth.is_accountant(None)
    finally:
        ops.access_ids = old


@check("an access dict without accountants (old cache, old tests) doesn't crash")
def _():
    old = ops.access_ids
    ops.access_ids = lambda: {"allowed": set(), "admins": set(), "approvers": set()}
    try:
        assert auth.is_accountant({"id": "c1"}) is False
    finally:
        ops.access_ids = old


# ---- reading old rows --------------------------------------------------

@check("a row from before the lifecycle reads as Sent if it was emailed, else Draft")
def _():
    assert ops._invoice_status(page()["properties"]) == "Draft"
    assert ops._invoice_status(page(sent_at="2026-10-02")["properties"]) == "Sent"
    assert ops._invoice_status(page(status="Paid")["properties"]) == "Paid"
    assert ops._invoice_status(page(status="Weird")["properties"]) == "Draft"


@check("the accountant never sees drafts or voided bills")
def _():
    acct = {"id": "c1"}
    old = ops.access_ids
    ops.access_ids = lambda: {"allowed": {"c1"}, "admins": set(), "approvers": set(),
                              "accountants": {"c1"}}
    try:
        for st, seen in (("Draft", False), ("Void", False), ("Approved", True),
                         ("Sent", True), ("Paid", True)):
            assert webapp._visible_invoice(acct, {"status": st}) is seen, st
        assert webapp._visible_invoice({"id": "nobody"}, {"status": "Paid"}) is False
    finally:
        ops.access_ids = old


@check("a sent bill past its due date is Overdue on the list")
def _():
    today = dt.date(2026, 10, 23)
    assert webapp._invoice_stage({"status": "Sent", "due": "2026-10-22"}, today) == "Overdue"
    assert webapp._invoice_stage({"status": "Sent", "due": "2026-10-23"}, today) == "Sent"
    assert webapp._invoice_stage({"status": "Paid", "due": "2026-01-01"}, today) == "Paid"


# ---- moving ------------------------------------------------------------

@check("marking sent sets the status, the due date and a history line")
def _():
    with fake_notion(page(status="Approved")) as f:
        ops.set_invoice_status("inv1", "Sent", "Angie", accountant=True,
                               sent_on="2026-10-01", sent_to="client@example.com")
    p = f.props
    assert p[ops.STATUS_PROP] == {"select": {"name": "Sent"}}
    assert p[ops.SENT_AT_PROP]["date"]["start"] == "2026-10-01"
    due = dt.date(2026, 10, 1) + dt.timedelta(days=ops.invoice_due_days())
    assert p[ops.DUE_PROP]["date"]["start"] == due.isoformat()
    hist = p[ops.HISTORY_PROP]["rich_text"][0]["text"]["content"]
    assert "Sent (due" in hist and "Angie" in hist


@check("a due date before the send date is refused, and nothing is written")
def _():
    with fake_notion(page(status="Approved")) as f:
        refused(lambda: ops.set_invoice_status("inv1", "Sent", accountant=True,
                                               sent_on="2026-10-10", due="2026-10-01"),
                ValueError)
    assert f.updates == []


@check("marking paid records the day and reference; undoing clears both")
def _():
    with fake_notion(page(status="Sent", due="2026-11-01")) as f:
        ops.set_invoice_status("inv1", "Paid", "Angie", accountant=True,
                               paid_on="2026-10-20", payment_ref="  wire   88213 ")
    assert f.props[ops.PAID_ON_PROP]["date"]["start"] == "2026-10-20"
    assert f.props[ops.PAYMENT_REF_PROP]["rich_text"][0]["text"]["content"] == "wire 88213"
    with fake_notion(page(status="Paid")) as f:
        ops.set_invoice_status("inv1", "Sent", "Angie", accountant=True)
    assert f.props[ops.PAID_ON_PROP] == {"date": None}
    assert f.props[ops.PAYMENT_REF_PROP] == {"rich_text": []}


@check("a refused move writes nothing")
def _():
    with fake_notion(page(status="Draft")) as f:
        msg = refused(lambda: ops.set_invoice_status("inv1", "Approved", accountant=True),
                      PermissionError)
        assert "admin" in msg
        refused(lambda: ops.set_invoice_status("inv1", "Paid", admin=True), PermissionError)
    assert f.updates == []


@check("history appends rather than replaces, and stays bounded")
def _():
    old = "\n".join(f"line {i}" for i in range(80))
    with fake_notion(page(status="Draft", history=old)) as f:
        ops.set_invoice_status("inv1", "Approved", "JP", admin=True)
    text = "".join(c["text"]["content"] for c in f.props[ops.HISTORY_PROP]["rich_text"])
    lines = text.split("\n")
    assert len(lines) == ops._MAX_HISTORY_LINES
    assert lines[-1].endswith("Approved · JP") and lines[0] == "line 21"


# ---- what a status locks -----------------------------------------------

@check("a sent, paid or voided bill can't be edited")
def _():
    for st in ("Sent", "Paid", "Void"):
        with fake_notion(page(status=st)) as f:
            refused(lambda: ops.set_invoice_bill("inv1", ["Work"], 10, 1000), ops.InvoiceLocked)
        assert f.updates == [], st


@check("editing an approved bill sends it back to Draft; a draft stays a draft")
def _():
    with fake_notion(page(status="Approved")) as f:
        out = ops.set_invoice_bill("inv1", ["Work"], 10, 1000, actor="JP")
    assert f.props[ops.STATUS_PROP] == {"select": {"name": "Draft"}}
    assert out["status"] == "Draft"
    with fake_notion(page(status="Draft")) as f:
        out = ops.set_invoice_bill("inv1", ["Work"], 10, 1000)
    assert ops.STATUS_PROP not in f.props and out["status"] == "Draft"


@check("emailing a bill marks it Sent and starts the due clock once")
def _():
    with fake_notion(page(status="Approved")) as f:
        ops.mark_invoice_sent("inv1", ["client@example.com"], "Angie")
    assert f.props[ops.STATUS_PROP] == {"select": {"name": "Sent"}}
    assert ops.DUE_PROP in f.props
    with fake_notion(page(status="Sent", due="2026-11-01")) as f:   # a resend
        ops.mark_invoice_sent("inv1", ["client@example.com"])
    assert ops.DUE_PROP not in f.props


@check("an email landing after a bill was marked paid doesn't un-pay it")
def _():
    with fake_notion(page(status="Paid")) as f:
        out = ops.mark_invoice_sent("inv1", ["client@example.com"], "Angie")
    assert ops.STATUS_PROP not in f.props and out["status"] == "Paid"
    assert "while paid" in f.props[ops.HISTORY_PROP]["rich_text"][0]["text"]["content"]


@check("an admin emailing a draft is recorded as skipping approval")
def _():
    with fake_notion(page(status="Draft")) as f:
        ops.mark_invoice_sent("inv1", ["client@example.com"], "JP")
    assert f.props[ops.STATUS_PROP] == {"select": {"name": "Sent"}}
    assert "without approval" in f.props[ops.HISTORY_PROP]["rich_text"][0]["text"]["content"]


@check("downloading the PDF of an approved invoice with nothing typed doesn't un-approve it")
def _():
    # The bug: no stored lines/amount, so the page's prefilled defaults looked
    # like an edit, and the save-before-download sent it back to Draft.
    invoice = {"id": "inv1", "project_id": "p1", "project": "Eloquence", "month": "2026-10-01",
               "hours_tracked": 15.5, "hours_billed": 15.5, "rate": 0, "amount": None,
               "currency": "USD", "lines": [], "status": "Approved", "history": []}
    shown = webapp._invoice_doc(invoice)
    calls = []
    old = ops.set_invoice_bill
    ops.set_invoice_bill = lambda *a, **k: calls.append((a, k))
    try:
        req = webapp.InvoiceBill(invoice_id="inv1", lines="\n".join(shown["lines"]),
                                 hours=15.5, amount=shown["amount"])
        out = webapp._apply_bill(req, dict(invoice))
        assert calls == [] and out["status"] == "Approved", (calls, out)
        # a real edit still writes
        req = webapp.InvoiceBill(invoice_id="inv1", lines="Something else", hours=15.5, amount=0)
        webapp._apply_bill(req, dict(invoice))
        assert len(calls) == 1
    finally:
        ops.set_invoice_bill = old


@check("the status bar only offers moves this viewer may make")
def _():
    old = ops.access_ids
    ops.access_ids = lambda: {"allowed": {"c1", "a1"}, "admins": {"a1"}, "approvers": set(),
                              "accountants": {"c1"}}
    try:
        acct = [m["to"] for m in webapp._invoice_moves({"id": "c1"}, {"status": "Approved"})]
        assert acct == ["Sent"], acct
        admin = [m["to"] for m in webapp._invoice_moves({"id": "a1"}, {"status": "Approved"})]
        assert set(admin) == {"Draft", "Sent", "Void"}, admin
        assert webapp._invoice_moves({"id": "c1"}, {"status": "Draft"}) == []
    finally:
        ops.access_ids = old


if __name__ == "__main__":
    print("invoice lifecycle")
    if _FAILS:
        print(f"\n{len(_FAILS)} failed: {', '.join(_FAILS)}")
        sys.exit(1)
    print("\nall passed")
