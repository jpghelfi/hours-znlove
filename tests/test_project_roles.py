#!/usr/bin/env python3
"""Tests for project roles (PM / Account manager) and their filters.

Run:  ./.venv/bin/python tests/test_project_roles.py

Plain asserts and a tiny runner, same shape as tests/test_budgets.py — no
pytest dependency, and nothing here touches Notion (the module-level client is
built at import, so a NOTION_TOKEN must exist in .env, but every function that
would make a call is monkeypatched).
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


# ---- parsing -------------------------------------------------------------

@check("no PM/Account manager column parses as unset")
def _():
    assert ops._role_from_props({}, "pm") is None
    assert ops._role_from_props({}, "am") is None


@check("a renamed role column reads as unset, never as a crash")
def _():
    # The alloc_person_prop lesson: a stray rename in Notion must not 500 the
    # page — the project just stops being filterable by that role.
    assert ops._role_from_props({"Manager": {"people": [{"id": "u1"}]}}, "pm") is None


@check("one person in the column reads through cleanly")
def _():
    props = {ops.AM_PROP: {"people": [{"id": "u9"}]}}
    assert ops._role_from_props(props, "am") == "u9"


@check("two people in the column: only the first is read")
def _():
    # Notion has no single-person property type; the app treats the column as
    # holding at most one and a second person added by hand is ignored.
    props = {ops.PM_PROP: {"people": [{"id": "u1"}, {"id": "u2"}]}}
    assert ops._role_from_props(props, "pm") == "u1"


# ---- _role_picks -----------------------------------------------------------

@check("_role_picks drops ids that aren't on the roster")
def _():
    people = [{"id": "u1", "name": "Ana"}, {"id": "u2", "name": "Beto"}]
    pm_ids, am_ids = webapp._role_picks(people, ["u1", "ghost"], ["u2", "ghost"])
    assert pm_ids == {"u1"}
    assert am_ids == {"u2"}


@check("_role_picks: order doesn't matter and duplicates collapse (set semantics)")
def _():
    people = [{"id": "u1", "name": "Ana"}, {"id": "u2", "name": "Beto"}]
    pm_ids, _ = webapp._role_picks(people, ["u2", "u1", "u1"], [])
    assert pm_ids == {"u1", "u2"}


@check("_role_picks: no picks are empty sets")
def _():
    pm_ids, am_ids = webapp._role_picks([{"id": "u1", "name": "Ana"}], [], [])
    assert pm_ids == set()
    assert am_ids == set()


# ---- _role_match -----------------------------------------------------------

@check("_role_match: no pick in either role matches every project")
def _():
    assert webapp._role_match({"pm_id": None, "am_id": None}, set(), set()) is True
    assert webapp._role_match({"pm_id": "u1", "am_id": "u2"}, set(), set()) is True


@check("_role_match: several picks within a role are OR")
def _():
    proj = {"pm_id": "u2", "am_id": None}
    assert webapp._role_match(proj, {"u1", "u2"}, set()) is True
    assert webapp._role_match(proj, {"u1"}, set()) is False


@check("_role_match: the two roles combine with AND")
def _():
    proj = {"pm_id": "u1", "am_id": "u3"}
    assert webapp._role_match(proj, {"u1"}, {"u3"}) is True
    # right PM, wrong account manager -> excluded
    assert webapp._role_match(proj, {"u1"}, {"u4"}) is False


@check("_role_match: a PM pick excludes a project with no PM, but leaving it unpicked keeps it")
def _():
    proj = {"pm_id": None, "am_id": None}
    assert webapp._role_match(proj, {"u1"}, set()) is False
    assert webapp._role_match(proj, set(), set()) is True


@check("_role_keep_ids is None with no picks, else the matching id set")
def _():
    projects = [{"id": "a", "pm_id": "u1", "am_id": None},
                {"id": "b", "pm_id": "u2", "am_id": None}]
    assert webapp._role_keep_ids(projects, set(), set()) is None
    assert webapp._role_keep_ids(projects, {"u1"}, set()) == {"a"}


# ---- set_project_role -------------------------------------------------------

@check("set_project_role refuses an unknown role")
def _():
    try:
        ops.set_project_role("proj1", "owner", "u1")
        assert False, "should have raised"
    except ValueError as exc:
        assert "role" in str(exc)


@check("set_project_role refuses a person who isn't on the roster")
def _():
    old = ops.list_people
    ops.list_people = lambda: [{"id": "u1", "name": "Ana"}]
    try:
        try:
            ops.set_project_role("proj1", "pm", "ghost")
            assert False, "should have raised"
        except ValueError as exc:
            assert "roster" in str(exc)
    finally:
        ops.list_people = old


# ---- reports narrowing -------------------------------------------------

@check("reports: a role pick narrows entries and planned rows to matching projects")
def _():
    people = [{"id": "u1", "name": "Ana"}, {"id": "u2", "name": "Beto"}]
    projects = [
        {"id": "pA", "name": "A", "pm_id": "u1", "am_id": None},
        {"id": "pB", "name": "B", "pm_id": "u2", "am_id": None},
    ]
    entries = [
        {"project_id": "pA", "project": "A", "person_id": "u1", "person": "Ana",
         "date": "2026-08-05", "hours": 3, "goal": ""},
        {"project_id": "pB", "project": "B", "person_id": "u2", "person": "Beto",
         "date": "2026-08-05", "hours": 2, "goal": ""},
    ]
    planned = [
        {"project_id": "pA", "project": "A", "person_id": "u1", "person": "Ana", "hours": 1},
        {"project_id": "pB", "project": "B", "person_id": "u2", "person": "Beto", "hours": 4},
    ]
    old = (ops.list_people, ops.list_projects, ops.entries_between,
           ops.planned_rows, webapp.auth.is_admin)
    ops.list_people = lambda: people
    ops.list_projects = lambda active_only=True: projects
    ops.entries_between = lambda f, t, person_id=None: list(entries)
    ops.planned_rows = lambda f, t, person_id=None: list(planned)
    webapp.auth.is_admin = lambda user: True
    try:
        data = webapp._report_data({"id": "u1", "email": "a@x.com"}, "team",
                                    "this-week", None, None, people=None,
                                    pm=["u1"], am=None)
        # only project A's entry survives
        assert [e["project_id"] for e in data["entries"]] == ["pA"], data["entries"]
        assert data["total"] == 3
        # project B's planned hours are gone too — pva only ever mentions A
        assert [p["name"] for p in data["pva"]] == ["A"], data["pva"]
        assert data["pm_selected"] == {"u1"}
        assert data["am_selected"] == set()
    finally:
        (ops.list_people, ops.list_projects, ops.entries_between,
         ops.planned_rows, webapp.auth.is_admin) = old


@check("reports: no role pick leaves every project's rows in place")
def _():
    people = [{"id": "u1", "name": "Ana"}]
    projects = [{"id": "pA", "name": "A", "pm_id": "u1", "am_id": None},
                {"id": "pB", "name": "B", "pm_id": None, "am_id": None}]
    entries = [
        {"project_id": "pA", "project": "A", "person_id": "u1", "person": "Ana",
         "date": "2026-08-05", "hours": 3, "goal": ""},
        {"project_id": "pB", "project": "B", "person_id": "u1", "person": "Ana",
         "date": "2026-08-05", "hours": 2, "goal": ""},
    ]
    old = (ops.list_people, ops.list_projects, ops.entries_between,
           ops.planned_rows, webapp.auth.is_admin)
    ops.list_people = lambda: people
    ops.list_projects = lambda active_only=True: projects
    ops.entries_between = lambda f, t, person_id=None: list(entries)
    ops.planned_rows = lambda f, t, person_id=None: []
    webapp.auth.is_admin = lambda user: True
    try:
        data = webapp._report_data({"id": "u1"}, "team", "this-week", None, None)
        assert {e["project_id"] for e in data["entries"]} == {"pA", "pB"}
        assert data["total"] == 5
    finally:
        (ops.list_people, ops.list_projects, ops.entries_between,
         ops.planned_rows, webapp.auth.is_admin) = old


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
