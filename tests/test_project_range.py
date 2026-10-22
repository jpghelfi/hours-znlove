#!/usr/bin/env python3
"""Tests for /project's custom date range and archived projects.

Run:  ./.venv/bin/python tests/test_project_range.py

Same shape as the other tests — plain asserts, a tiny runner, no pytest, and
nothing here touches Notion (every function that would make a call is
monkeypatched).
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

from web import notion_ops as ops  # noqa: E402
from web import app as webapp  # noqa: E402

_FAILS: list[str] = []


def check(name):
    def deco(fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception:
            print(f"  FAIL {name}")
            _FAILS.append(name + "\n" + traceback.format_exc())
        return fn
    return deco


D = dt.date


@check("custom range keeps its bounds and steps by its own length")
def _():
    period, r = webapp._project_range("custom", "2026-09-01", "2026-09-10")
    assert period == "custom"
    assert (r["from"], r["to"]) == ("2026-09-01", "2026-09-10")
    assert (r["prev"], r["prev_end"]) == ("2026-08-22", "2026-08-31")
    assert (r["next"], r["next_end"]) == ("2026-09-11", "2026-09-20")
    assert "10 days" in r["label"]


@check("reversed bounds are swapped, not emptied")
def _():
    _, r = webapp._project_range("custom", "2026-09-10", "2026-09-01")
    assert (r["from"], r["to"]) == ("2026-09-01", "2026-09-10")


@check("an over-long range is clipped to a year")
def _():
    _, r = webapp._project_range("custom", "2020-01-01", "2026-01-01")
    span = (D.fromisoformat(r["to"]) - D.fromisoformat(r["from"])).days + 1
    assert span == webapp._MAX_CUSTOM_DAYS


@check("no bounds = month to date")
def _():
    _, r = webapp._project_range("custom", None, None)
    today = D.today()
    assert r["from"] == today.replace(day=1).isoformat()
    assert r["to"] == today.isoformat()


@check("'To today' is the same length ending today")
def _():
    _, r = webapp._project_range("custom", "2026-01-01", "2026-01-07")
    assert r["now_end"] == D.today().isoformat()
    assert (D.fromisoformat(r["now_end"]) - D.fromisoformat(r["now"])).days == 6


@check("the old periods still read as before, unknown ones fall back to monthly")
def _():
    period, r = webapp._project_range("weekly", "2026-10-22", "2027-01-01")
    assert period == "weekly" and (r["from"], r["to"]) == ("2026-10-19", "2026-10-25")
    period, r = webapp._project_range("bogus", "2026-10", None)
    assert period == "monthly" and r["from"] == "2026-10-01"


PROJECTS = [
    {"id": "live", "name": "Live", "active": True, "member_ids": []},
    {"id": "old", "name": "Old", "active": False, "member_ids": []},
    {"id": "idle", "name": "Idle archived", "active": False, "member_ids": []},
]
ENTRIES = [
    {"id": "e1", "project_id": "old", "project": "Old", "person_id": "u1",
     "person": "Ana", "hours": 3.0, "date": "2026-09-02", "description": "x"},
]


def _rollup(keep_ids=None):
    old = ops.entries_between
    ops.entries_between = lambda f, t, *a, **k: list(ENTRIES)
    try:
        rng = {"from": "2026-09-01", "to": "2026-09-30"}
        return webapp._all_projects_hours(rng, PROJECTS, {"u1": "Ana"}, keep_ids)
    finally:
        ops.entries_between = old


@check("rollup keeps an archived project's hours, hides idle archived ones")
def _():
    names = {g["project_name"]: g["hours"] for g in _rollup()["groups"]}
    assert names.get("Old") == 3.0
    assert "Live" in names           # active, zero-filled as before
    assert "Idle archived" not in names


@check("an archived project picked outright shows even with no hours")
def _():
    names = [g["project_name"] for g in _rollup({"idle"})["groups"]]
    assert names == ["Idle archived"]


@check("archived projects resolve as a pick")
def _():
    sel_ids, sel = webapp._project_picks(PROJECTS, ["old"])
    assert sel_ids == ["old"] and sel["name"] == "Old"


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
