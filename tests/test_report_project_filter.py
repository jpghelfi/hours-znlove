#!/usr/bin/env python3
"""Tests for /reports' project filter (repeated ?project=).

Run:  ./.venv/bin/python tests/test_report_project_filter.py

Same shape as tests/test_project_roles.py — plain asserts, a tiny runner, no
pytest, and nothing here touches Notion (every function that would make a call
is monkeypatched).
"""
from __future__ import annotations

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


PEOPLE = [{"id": "u1", "name": "Ana"}, {"id": "u2", "name": "Beto"}]
PROJECTS = [
    {"id": "pA", "name": "A", "active": True, "pm_id": "u1", "am_id": None},
    {"id": "pB", "name": "B", "active": True, "pm_id": "u2", "am_id": None},
    {"id": "pC", "name": "C", "active": False, "pm_id": "u1", "am_id": None},
]
ENTRIES = [
    {"project_id": "pA", "project": "A", "person_id": "u1", "person": "Ana",
     "date": "2026-08-05", "hours": 3, "goal": ""},
    {"project_id": "pB", "project": "B", "person_id": "u2", "person": "Beto",
     "date": "2026-08-05", "hours": 2, "goal": ""},
    {"project_id": "pC", "project": "C", "person_id": "u1", "person": "Ana",
     "date": "2026-08-05", "hours": 5, "goal": ""},
]
PLANNED = [
    {"project_id": "pA", "project": "A", "person_id": "u1", "person": "Ana", "hours": 1},
    {"project_id": "pB", "project": "B", "person_id": "u2", "person": "Beto", "hours": 4},
]


def report(**kw):
    """_report_data with Notion stubbed out and the caller an admin."""
    old = (ops.list_people, ops.list_projects, ops.entries_between,
           ops.planned_rows, webapp.auth.is_admin)
    ops.list_people = lambda: PEOPLE
    ops.list_projects = lambda active_only=True, **_: [
        p for p in PROJECTS if p["active"] or not active_only]
    ops.entries_between = lambda f, t, person_id=None: [dict(e) for e in ENTRIES]
    ops.planned_rows = lambda f, t, person_id=None: [dict(p) for p in PLANNED]
    webapp.auth.is_admin = lambda user: True
    try:
        return webapp._report_data({"id": "u1", "email": "a@x.com"}, "team",
                                   "this-week", None, None, **kw)
    finally:
        (ops.list_people, ops.list_projects, ops.entries_between,
         ops.planned_rows, webapp.auth.is_admin) = old


# ---- the pick ------------------------------------------------------------

@check("no project pick leaves every project's rows in place")
def _():
    data = report()
    assert {e["project_id"] for e in data["entries"]} == {"pA", "pB", "pC"}
    assert data["total"] == 10
    assert data["project_selected"] == []


@check("one project narrows entries and planned rows to it")
def _():
    data = report(projects=["pA"])
    assert [e["project_id"] for e in data["entries"]] == ["pA"]
    assert data["total"] == 3
    # project B's scheduled hours are gone too — pva only ever mentions A
    assert [p["name"] for p in data["pva"]] == ["A"]
    assert data["project_selected"] == ["pA"]


@check("several projects roll up just those")
def _():
    data = report(projects=["pA", "pB"])
    assert {e["project_id"] for e in data["entries"]} == {"pA", "pB"}
    assert data["total"] == 5


@check("an archived project can still be picked")
def _():
    # an old entry's project may have been unticked in Notion since; the filter
    # reads the full list so its hours stay reachable
    data = report(projects=["pC"])
    assert data["total"] == 5


@check("a stale id degrades to the unfiltered page, not to an empty one")
def _():
    data = report(projects=["ghost"])
    assert data["total"] == 10
    assert data["project_selected"] == []


@check("the legacy 'all' sentinel means every project")
def _():
    data = report(projects=["all"])
    assert data["total"] == 10
    assert data["project_selected"] == []


@check("duplicate ids collapse and order is the query's")
def _():
    data = report(projects=["pB", "pA", "pB"])
    assert data["project_selected"] == ["pB", "pA"]
    assert data["total"] == 5


# ---- composition ---------------------------------------------------------

@check("project and role picks compose with AND")
def _():
    # Ana PMs A and C; narrowing to A and B leaves only A
    data = report(projects=["pA", "pB"], pm=["u1"])
    assert [e["project_id"] for e in data["entries"]] == ["pA"]
    assert data["total"] == 3


@check("a project pick disjoint from the role pick empties the page, not the filter")
def _():
    # Beto PMs only B; asking for A as well as the role narrows to nothing
    data = report(projects=["pA"], pm=["u2"])
    assert data["entries"] == []
    assert data["total"] == 0
    assert data["project_selected"] == ["pA"]


@check("project and people picks compose with AND")
def _():
    data = report(projects=["pA", "pB"], people=["u2"])
    assert [e["project_id"] for e in data["entries"]] == ["pB"]
    assert data["total"] == 2


@check("a caller-supplied project list is used instead of a second Notion read")
def _():
    # /reports fills the picker from list_projects anyway and hands the list in;
    # reading it again per report would double the page's cost for nothing
    calls = []
    old = (ops.list_people, ops.list_projects, ops.entries_between,
           ops.planned_rows, webapp.auth.is_admin)
    ops.list_people = lambda: PEOPLE
    ops.list_projects = lambda *a, **k: calls.append(1) or []
    ops.entries_between = lambda f, t, person_id=None: [dict(e) for e in ENTRIES]
    ops.planned_rows = lambda f, t, person_id=None: []
    webapp.auth.is_admin = lambda user: True
    try:
        data = webapp._report_data({"id": "u1"}, "team", "this-week", None, None,
                                   projects=["pA"], project_list=PROJECTS)
    finally:
        (ops.list_people, ops.list_projects, ops.entries_between,
         ops.planned_rows, webapp.auth.is_admin) = old
    assert calls == [], "list_projects should not have been read"
    assert data["total"] == 3


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
