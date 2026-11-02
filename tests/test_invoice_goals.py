#!/usr/bin/env python3
"""Tests for the invoice page's goal grouping (it replaced the per-day one).

Run:  ./.venv/bin/python tests/test_invoice_goals.py

Plain asserts and a tiny runner, like the other suites. Nothing touches Notion.
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
    {"id": "e1", "date": "2026-10-01", "person": "Ana", "hours": 1.0, "description": "deploy", "goal": "Maintenance"},
    {"id": "e2", "date": "2026-10-02", "person": "Bo", "hours": 3.0, "description": "hero", "goal": "Homepage"},
    {"id": "e3", "date": "2026-10-05", "person": "Ana", "hours": 9.0, "description": "misc", "goal": ""},
    {"id": "e4", "date": "2026-10-06", "person": "Bo", "hours": 2.0, "description": "patch", "goal": "Maintenance"},
]


def invoice(**kw):
    base = {"id": "inv1", "project_id": "p1", "project": "Kepos", "month": "2026-10-01",
            "status": "Draft", "hours_billed": 15.0, "hours_tracked": 15.0, "rate": None,
            "amount": None, "adjustments": {}, "comments": {}, "history": []}
    base.update(kw)
    return base


@check("_invoiced_goals: biggest goal first, No goal last, totals per goal")
def _():
    rows = webapp._invoiced_rows(invoice(adjustments={"e2": 5.0}), ENTRIES)
    goals = webapp._invoiced_goals(rows)
    assert [g["label"] for g in goals] == ["Homepage", "Maintenance", "No goal"], goals
    home, maint, none = goals
    assert (home["tracked"], home["billed"]) == (3.0, 5.0)
    assert (maint["tracked"], maint["billed"]) == (3.0, 3.0)
    assert [r["id"] for r in maint["rows"]] == ["e1", "e4"]   # date order inside
    assert none["billed"] == 9.0                              # last despite being biggest


@check("/invoices/{id} renders goal groups and the filter, no day sections")
def _():
    old = (ops.get_invoice, webapp._period_entries, ops.access_ids, ops.project_billing)
    ops.get_invoice = lambda i: invoice()
    webapp._period_entries = lambda *a, **k: [dict(e) for e in ENTRIES]
    ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": {"u-admin"},
                              "approvers": set(), "accountants": set()}
    ops.project_billing = lambda pid: {}
    try:
        from fastapi.testclient import TestClient
        r = TestClient(webapp.app).get("/invoices/inv1")
    finally:
        ops.get_invoice, webapp._period_entries, ops.access_ids, ops.project_billing = old
    assert r.status_code == 200, r.text[:500]
    html = r.text
    Path(os.environ.get("RENDER_OUT", "/dev/null")).write_text(html)
    assert 'id="goalpick"' in html
    assert "Maintenance · 3 h" in html and "No goal · 9 h" in html
    assert html.count('class="group-row"') == 3
    assert "Thu 01 Oct" in html and html.count('data-line="e') == 4


if __name__ == "__main__":
    if _FAILS:
        print(f"\n{len(_FAILS)} failed")
        sys.exit(1)
    print("\nall passed")
