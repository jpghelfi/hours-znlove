#!/usr/bin/env python3
"""Tests for the Goal column in the exports (workbook and Google Sheet).

Run:  ./.venv/bin/python tests/test_export_goal.py

Plain asserts and a tiny runner, same shape as the other suites here. Nothing
touches Notion or Google: only the pure row builders are exercised —
report_gsheet.create() is the part that calls out, and it is not called.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-for-anything")

from web import report_gsheet, report_xlsx  # noqa: E402

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


def entry(hours, goal="", person="Ana", date="2026-08-10", project="Kepos",
          project_id="p1", description="did a thing"):
    return {"project_id": project_id, "project": project, "person_id": "u1",
            "person": person, "date": date, "hours": hours,
            "description": description, "task": "", "task_url": "", "goal": goal}


def tab(entries):
    return report_gsheet._project_tab(report_xlsx.group(entries)[0], "August 2026")


def find(rows, first):
    return next(r for r in rows if r and r[0] == first)


# ---- the sheet's log -----------------------------------------------------

@check("a project that uses goals gets a Goal column in the log")
def _():
    rows = tab([entry(2, "Maintenance"), entry(3, "New PDP")])
    head = find(rows, "Date")
    assert head == ["Date", "Person", "Hours", "Goal", "Comment", "Ticket"], head
    log = rows[rows.index(head) + 1:]
    assert [r[3] for r in log] == ["Maintenance", "New PDP"], log


@check("a project without goals gets exactly the sheet it had before")
def _():
    rows = tab([entry(2), entry(3)])
    head = find(rows, "Date")
    assert head == ["Date", "Person", "Hours", "Comment", "Ticket"], head
    assert not any(r and r[0] == "By goal" for r in rows)


@check("an entry with no goal reads as blank, not as a missing cell")
def _():
    # one goal on the project is enough for the column, and a row without one
    # still has to line up under Comment
    rows = tab([entry(2, "Maintenance"), entry(3)])
    head = find(rows, "Date")
    log = rows[rows.index(head) + 1:]
    assert all(len(r) == len(head) for r in log), log
    assert [r[3] for r in log] == ["Maintenance", ""], log


@check("the ticket stays in the last column once Goal is inserted")
def _():
    e = entry(2, "Maintenance")
    e["task"], e["task_url"] = "TICK-1", "https://www.notion.so/x-abc123"
    rows = tab([e])
    head = find(rows, "Date")
    log = rows[rows.index(head) + 1:]
    assert log[0][-1].startswith("=HYPERLINK("), log[0]
    assert log[0][4] == "did a thing"


# ---- the sheet's by-goal block ------------------------------------------

@check("the By goal block sits above the log, hours desc")
def _():
    rows = tab([entry(2, "Maintenance"), entry(5, "New PDP"), entry(1, "Maintenance")])
    names = [r[0] for r in rows if r]
    assert names.index("By goal") < names.index("Log")
    head = find(rows, "Goal")
    block = rows[rows.index(head) + 1:]
    assert block[0] == ["New PDP", 5, 1], block[0]
    assert block[1] == ["Maintenance", 3, 2], block[1]
    assert block[2] == ["Total", 8.0], block[2]


@check("unassigned hours are a row in the block, always last")
def _():
    rows = tab([entry(1, "Maintenance"), entry(9)])
    head = find(rows, "Goal")
    block = rows[rows.index(head) + 1:]
    assert [r[0] for r in block[:2]] == ["Maintenance", "Unassigned"], block
    # it's last despite being the bigger number — the real one leads
    assert block[1][1] == 9


# ---- the two files agree -------------------------------------------------

@check("the sheet and the workbook agree on which projects show a Goal column")
def _():
    for entries in ([entry(2, "Maintenance")], [entry(2)]):
        g = report_xlsx.group(entries)[0]
        head = find(report_gsheet._project_tab(g, "August 2026"), "Date")
        assert ("Goal" in head) == bool(g["goals"]), (head, g["goals"])


@check("the workbook still builds, with and without goals")
def _():
    for entries in ([entry(2, "Maintenance")], [entry(2)]):
        blob = report_xlsx.build(entries, "August 2026", "Kepos")
        assert blob[:2] == b"PK", "not a zip — openpyxl produced nothing usable"


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
