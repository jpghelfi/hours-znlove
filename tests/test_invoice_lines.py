#!/usr/bin/env python3
"""Tests for editing an invoice line by line (billed hours + wording).

Run:  ./.venv/bin/python tests/test_invoice_lines.py

Plain asserts and a tiny runner, like the other suites. Nothing touches
Notion: reads are stubbed, writes are recorded.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-for-anything")
os.environ["AUTH_DISABLED"] = "1"
os.environ["DEV_USER_ID"] = "u-admin"
os.environ["ADMIN_EMAILS"] = "dev@local"

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


ENTRIES = [
    {"id": "e1", "date": "2026-09-01", "person": "Ana", "hours": 4.0, "description": "homepage"},
    {"id": "e2", "date": "2026-09-02", "person": "Bo", "hours": 3.0, "description": "fixes"},
]


def invoice(**kw):
    base = {"id": "inv1", "project_id": "p1", "project": "Kepos", "month": "2026-09-01",
            "status": "Draft", "hours_billed": 7.0, "hours_tracked": 7.0, "rate": 100.0,
            "amount": None, "adjustments": {}, "comments": {}, "history": []}
    base.update(kw)
    return base


@check("_invoiced_rows lays reworded comments over the logged ones and keeps both")
def _():
    rows = webapp._invoiced_rows(invoice(comments={"e1": "Homepage redesign"},
                                         adjustments={"e2": 2.0}), ENTRIES)
    a, b = rows
    assert a["description"] == "Homepage redesign" and a["logged_description"] == "homepage"
    assert a["reworded"] and not a["changed"]
    assert b["description"] == "fixes" and not b["reworded"] and b["billed"] == 2.0 and b["changed"]


@check("_invoice_comments stores only lines whose wording changed")
def _():
    logged = {"e1": "homepage", "e2": "fixes"}
    out = webapp._invoice_comments([{"id": "e1", "description": " homepage "},
                                    {"id": "e2", "description": "Bug fixes"},
                                    {"id": "zz", "description": "not ours"},
                                    {"id": "e1"}], logged)
    assert out == {"e2": "Bug fixes"}


class stubs:
    """Stub the reads the endpoint makes; record what it would write."""

    def __init__(self, inv):
        self.inv, self.calls = inv, []

    def __enter__(self):
        self.old = (ops.get_invoice, webapp._period_entries, ops.set_invoice_lines, ops.access_ids)
        ops.get_invoice = lambda i: self.inv
        webapp._period_entries = lambda *a, **k: [dict(e) for e in ENTRIES]

        def record(iid, adj, com, hours, amount, actor=""):
            self.calls.append((adj, com, hours, amount))
            out = dict(self.inv, hours_billed=hours, status="Draft")
            if amount is not None:
                out["amount"] = amount
            return out
        ops.set_invoice_lines = record
        ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": {"u-admin"},
                                  "approvers": set(), "accountants": set()}
        return self

    def __exit__(self, *exc):
        ops.get_invoice, webapp._period_entries, ops.set_invoice_lines, ops.access_ids = self.old


def post(rows, inv_id="inv1"):
    from fastapi.testclient import TestClient
    return TestClient(webapp.app).post("/api/invoice/lines", json={"invoice_id": inv_id, "rows": rows})


@check("saving lines stores the differences, totals follow, amount follows hours × rate")
def _():
    with stubs(invoice()) as s:
        r = post([{"id": "e1", "hours": 3.5, "description": "Homepage redesign"},
                  {"id": "e2", "hours": 3, "description": "fixes"}])
        assert r.status_code == 200, r.text
        adj, com, hours, amount = s.calls[0]
        assert adj == {"e1": 3.5} and com == {"e1": "Homepage redesign"}
        assert hours == 6.5 and amount == 650.0
        assert r.json()["hours"] == 6.5 and r.json()["amount"] == 650.0


@check("a hand-typed amount survives a line save")
def _():
    with stubs(invoice(amount=500.0)) as s:
        assert post([{"id": "e1", "hours": 2, "description": "homepage"}]).status_code == 200
        assert s.calls[0][3] is None and s.calls[0][2] == 5.0


@check("a line put back to what was logged stops being an edit; untouched lines keep theirs")
def _():
    with stubs(invoice(adjustments={"e1": 1.0, "e2": 2.0}, comments={"e1": "X"},
                       hours_billed=3.0)) as s:
        post([{"id": "e1", "hours": 4, "description": "homepage"}])
        adj, com, hours, _ = s.calls[0]
        assert adj == {"e2": 2.0} and com == {} and hours == 6.0


@check("a wording-only edit leaves the total alone — late entries and typed totals aren't re-billed")
def _():
    # billed 5 h by hand against 7 h logged (e.g. a flat bill, or e2 logged late)
    with stubs(invoice(hours_billed=5.0, amount=None)) as s:
        post([{"id": "e1", "hours": 4, "description": "Homepage redesign"}])
        adj, com, hours, amount = s.calls[0]
        assert hours == 5.0 and amount is None and com == {"e1": "Homepage redesign"}
    # and an hours edit moves it by exactly that line's change
    with stubs(invoice(hours_billed=5.0, amount=None)) as s:
        post([{"id": "e2", "hours": 1, "description": "fixes"}])
        assert s.calls[0][2] == 3.0 and s.calls[0][3] == 300.0


@check("an entry outside the month, bad hours, or a sent invoice are refused before writing")
def _():
    with stubs(invoice()) as s:
        assert post([{"id": "elsewhere", "hours": 1, "description": "x"}]).status_code == 400
        assert post([{"id": "e1", "hours": 30, "description": "x"}]).status_code == 400
        assert post([{"id": "e1", "hours": "nope", "description": "x"}]).status_code == 400
        assert s.calls == []
    with stubs(invoice(status="Sent")) as s:
        assert post([{"id": "e1", "hours": 1, "description": "x"}]).status_code == 409
        assert s.calls == []


@check("only admins may edit lines")
def _():
    with stubs(invoice()) as s:
        ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": set(),
                                  "approvers": set(), "accountants": set()}
        old = os.environ.pop("ADMIN_EMAILS")
        try:
            assert post([{"id": "e1", "hours": 1, "description": "x"}]).status_code == 403
        finally:
            os.environ["ADMIN_EMAILS"] = old
        assert s.calls == []


@check("set_invoice_lines sends an approved bill back to Draft and writes both maps")
def _():
    written = []

    class N:
        class pages:
            @staticmethod
            def update(pid, properties):
                written.append(properties)
    old = (ops._notion, ops.get_invoice)
    ops._notion = N
    ops.get_invoice = lambda i: invoice(status="Approved")
    try:
        out = ops.set_invoice_lines("inv1", {"e1": 3.5}, {"e1": "Redesign"}, 6.5, 650.0)
        props = written[0]
        assert props[ops.STATUS_PROP]["select"]["name"] == "Draft" and out["status"] == "Draft"
        assert props["Hours billed"]["number"] == 6.5 and props[ops.AMOUNT_PROP]["number"] == 650.0
        assert "Redesign" in props[ops._COMMENTS_PROP]["rich_text"][0]["text"]["content"]
    finally:
        ops._notion, ops.get_invoice = old


@check("line comments round-trip through the stored JSON")
def _():
    rt = ops._comments_prop({"e1": "Redesign — “quoted”"})[ops._COMMENTS_PROP]["rich_text"]
    props = {ops._COMMENTS_PROP: {"rich_text": [{"plain_text": c["text"]["content"]} for c in rt]}}
    assert ops._comments_from_props(props) == {"e1": "Redesign — “quoted”"}


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    sys.exit(1 if _FAILS else 0)
