#!/usr/bin/env python3
"""Tests for deleting an invoice (any status, admins only).

Run:  ./.venv/bin/python tests/test_invoice_delete.py

Plain asserts and a tiny runner; nothing touches Notion.
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


class fake:
    def __init__(self, invoice):
        self.invoice, self.updates = invoice, []

    def __enter__(self):
        outer = self

        class N:
            class pages:
                @staticmethod
                def update(pid, **kw):
                    outer.updates.append((pid, kw))
        self.old = (ops._notion, ops.get_invoice, ops.access_ids)
        ops._notion = N
        ops.get_invoice = lambda i: self.invoice
        ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": {"u-admin"},
                                  "approvers": set(), "accountants": set()}
        return self

    def __exit__(self, *exc):
        ops._notion, ops.get_invoice, ops.access_ids = self.old


INV = {"id": "inv1", "project": "Eloquence", "month": "2026-10-01", "status": "Sent"}


def post(body):
    from fastapi.testclient import TestClient
    return TestClient(webapp.app).post("/api/invoice/delete", json=body)


@check("an admin deletes a sent invoice: the page is archived, nothing else written")
def _():
    with fake(dict(INV)) as f:
        r = post({"invoice_id": "inv1"})
        assert r.status_code == 200 and r.json()["ok"], r.text
        assert f.updates == [("inv1", {"archived": True})]


@check("every status can be deleted")
def _():
    for st in ("Draft", "Approved", "Sent", "Paid", "Void"):
        with fake(dict(INV, status=st)) as f:
            assert post({"invoice_id": "inv1"}).status_code == 200, st
            assert f.updates == [("inv1", {"archived": True})]


@check("a page that isn't an invoice is refused before writing")
def _():
    with fake(None) as f:
        assert post({"invoice_id": "nope"}).status_code == 404
        assert f.updates == []


@check("non-admins (the accountant included) can't delete")
def _():
    with fake(dict(INV)) as f:
        ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": set(),
                                  "approvers": set(), "accountants": {"u-admin"}}
        old = os.environ.pop("ADMIN_EMAILS")
        try:
            assert post({"invoice_id": "inv1"}).status_code == 403
        finally:
            os.environ["ADMIN_EMAILS"] = old
        assert f.updates == []


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    sys.exit(1 if _FAILS else 0)
