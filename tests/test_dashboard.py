#!/usr/bin/env python3
"""Tests for the /dashboard arithmetic (web/dashboard.py).

Run:  ./.venv/bin/python tests/test_dashboard.py

Plain asserts and a tiny runner, like tests/test_budgets.py. Imports only
web.dashboard, so nothing here needs .env or touches Notion.
"""
from __future__ import annotations

import datetime as dt
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import dashboard as d  # noqa: E402

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
OCT = [D(2026, 10, n) for n in range(1, 32) if D(2026, 10, n).weekday() < 5]  # 22 weekdays


@check("elapsed weekdays: strictly before today; none for a future month, all for a past one")
def _():
    assert len(OCT) == 22
    assert d.elapsed_weekdays(OCT, D(2026, 10, 1)) == []
    assert d.elapsed_weekdays(OCT, D(2026, 9, 15)) == []
    assert d.elapsed_weekdays(OCT, D(2026, 11, 3)) == OCT
    mid = d.elapsed_weekdays(OCT, D(2026, 10, 7))   # Wed: Thu 1, Fri 2, Mon 5, Tue 6
    assert mid == [D(2026, 10, 1), D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 6)]


@check("forecast: none without pace, used once the month is over, else linear")
def _():
    assert d.forecast(10, 0, 22) is None
    assert d.forecast(10, 5, 0) is None
    assert d.forecast(30, 22, 22) == 30
    assert d.forecast(30, 25, 22) == 30
    assert d.forecast(10, 5, 22) == 44


@check("add_forecast: flags a budgeted row headed over, leaves unbudgeted alone, keeps order")
def _():
    rows = [{"tracked": 10, "budget": {"hours": 40}},
            {"tracked": 10, "budget": {"hours": 50}},
            {"tracked": 10, "budget": None}]
    out = d.add_forecast(rows, 5, 22)
    assert out is rows
    assert out[0]["forecast"] == 44 and out[0]["forecast_over"] and round(out[0]["forecast_pct"]) == 110
    assert not out[1]["forecast_over"]
    assert out[2]["forecast_pct"] is None and not out[2]["forecast_over"]


@check("a 0 h budget doesn't divide by zero")
def _():
    out = d.add_forecast([{"tracked": 2, "budget": {"hours": 0}}], 5, 22)
    assert out[0]["forecast_pct"] is None and out[0]["forecast_over"]


@check("day/week capacity: a person's own value wins over every default")
def _():
    assert d.day_cap(None, 8) == 8
    assert d.day_cap({"hours_per_day": None}, 8) == 8
    assert d.day_cap({"hours_per_day": 0}, 8) == 8
    assert d.day_cap({"hours_per_day": 6}, 8) == 6
    assert d.week_cap({"hours_per_day": 6}, 8, 40) == 30
    assert d.week_cap({}, 8, 40) == 40
    assert d.week_cap({}, 8, None) == 40
    assert d.week_cap({}, 7, None) == 35


@check("capacity never goes negative")
def _():
    assert d.person_capacity(8, 3, 5) == 0
    assert d.person_capacity(6, 10, 2) == 48


@check("utilization: to-date capacity, absences only inside the window, statuses, sort")
def _():
    elapsed = OCT[:10]
    people = [{"id": "a", "name": "Ana", "hours_per_day": 6},
              {"id": "b", "name": "Bo"},
              {"id": "c", "name": "Cy"}]
    absent = {"a": {OCT[0], OCT[15]}}          # one day off so far, one later
    rows = d.utilization_rows(people, {"a": 54, "b": 20}, absent, elapsed, OCT, 8)
    by = {r["id"]: r for r in rows}
    assert by["a"]["cap_to_date"] == 6 * 9 and by["a"]["cap_month"] == 6 * 20
    assert by["a"]["pct"] == 100 and by["a"]["status"] == "full" and by["a"]["custom"]
    assert by["b"]["cap_to_date"] == 80 and by["b"]["pct"] == 25 and by["b"]["status"] == "low"
    assert by["c"]["used"] == 0 and by["c"]["pct"] == 0
    assert [r["id"] for r in rows] == ["a", "b", "c"]


@check("utilization with no elapsed days has no percentage")
def _():
    rows = d.utilization_rows([{"id": "a", "name": "A"}], {}, {}, [], OCT, 8)
    assert rows[0]["pct"] is None and rows[0]["status"] is None


@check("missing time: logged and approved days off don't count; clean people are left out")
def _():
    elapsed = OCT[:5]
    people = [{"id": "a", "name": "Ana"}, {"id": "b", "name": "Bo"}, {"id": "c", "name": "Cy"}]
    logged = {"a": set(elapsed), "b": {elapsed[0]}}
    absent = {"b": {elapsed[1]}}
    rows = d.missing_rows(people, logged, absent, elapsed)
    assert [(r["id"], r["n"]) for r in rows] == [("c", 5), ("b", 3)]
    assert rows[1]["days"] == elapsed[2:]


@check("pipeline: uninvoiced first, rate 0 means no value, an invoice with no hours still shows")
def _():
    projects = [{"id": "p1", "name": "One", "billing": {"rate": 100, "currency": "USD"}},
                {"id": "p2", "name": "Two", "billing": {"rate": 0.0}},
                {"id": "p3", "name": "Three"},
                {"id": "p4", "name": "Four"}]
    tracked = {"p1": 10, "p2": 20}
    invoices = {("p1", "2026-10-01"): {"id": "i1", "status": "Draft"},
                ("p3", "2026-10-01"): {"id": "i3", "status": "Paid"},
                ("p2", "2026-09-01"): {"id": "old", "status": "Paid"}}
    rows = d.pipeline_rows(projects, tracked, invoices, "2026-10-01")
    assert [r["id"] for r in rows] == ["p2", "p1", "p3"]
    assert rows[0]["status"] == "none" and rows[0]["value"] is None
    assert rows[1]["value"] == 1000 and rows[1]["status"] == "Draft"
    assert rows[2]["hours"] == 0 and rows[2]["status"] == "Paid"


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
