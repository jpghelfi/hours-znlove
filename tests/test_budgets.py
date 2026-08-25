#!/usr/bin/env python3
"""Tests for the monthly project budgets.

Run:  ./.venv/bin/python tests/test_budgets.py

Plain asserts and a tiny runner rather than pytest — the project has no test
dependency and this shouldn't be the change that adds one.

**Nothing here touches Notion.** The module-level client is built at import
(so a NOTION_TOKEN must exist in .env), but every function that would make a
call is monkeypatched. The point is the arithmetic and the state machine: the
delta rule, the reduce-is-always-allowed rule, the once-a-month alert stamp —
the things that are wrong in a way a page load won't show you.
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


class fake_budget:
    """Swap in a budget + a month total for the duration of a with-block."""

    def __init__(self, budget, tracked=0.0):
        self.budget, self.tracked = budget, tracked

    def __enter__(self):
        self._b, self._t = ops.budget_for, ops.project_month_hours
        self._m = ops._project_name_map
        ops.budget_for = lambda pid: self.budget
        ops.project_month_hours = lambda pid, date: self.tracked
        # BudgetExceeded names the project in its message; keep that offline too
        ops._project_name_map = lambda: {"p": "Test Project"}
        return self

    def __exit__(self, *exc):
        ops.budget_for, ops.project_month_hours = self._b, self._t
        ops._project_name_map = self._m


def budget(hours=40, policy=None, overrun=0, warn=None, notified=""):
    props = {ops.BUDGET_PROP: {"number": hours}}
    if policy:
        props[ops.BUDGET_POLICY_PROP] = {"select": {"name": policy}}
    if overrun:
        props[ops.BUDGET_OVERRUN_PROP] = {"number": overrun}
    if warn is not None:
        props[ops.BUDGET_WARN_PROP] = {"number": warn}
    if notified:
        props[ops.BUDGET_NOTIFIED_PROP] = {"rich_text": [{"plain_text": notified}]}
    return ops._budget_from_props(props)


# ---- parsing -----------------------------------------------------------

@check("an unbudgeted project parses as None")
def _():
    assert ops._budget_from_props({}) is None
    assert ops._budget_from_props({ops.BUDGET_PROP: {"number": None}}) is None


@check("a budget of 0 is a real budget, not an absent one")
def _():
    # Harvest's documented footgun: blank means "not budgeted", 0 means "no
    # hours allowed". Collapsing the two would silently un-cap a project.
    b = budget(hours=0)
    assert b is not None and b["hours"] == 0


@check("defaults: Warn only, env warn %, no overrun")
def _():
    b = budget()
    assert b["policy"] == ops.POLICY_WARN
    assert b["overrun_pct"] == 0
    assert b["warn_pct"] == ops.default_warn_pct()
    assert b["warn_pct_set"] is False
    assert b["limit"] == 40


@check("overrun % widens the limit")
def _():
    assert budget(hours=40, overrun=10)["limit"] == 44
    assert budget(hours=40, overrun=0)["limit"] == 40


@check("an unknown policy degrades to Warn only rather than raising")
def _():
    props = {ops.BUDGET_PROP: {"number": 10},
             ops.BUDGET_POLICY_PROP: {"select": {"name": "Explode"}}}
    assert ops._budget_from_props(props)["policy"] == ops.POLICY_WARN


@check("a renamed budget column reads as unbudgeted, never as a crash")
def _():
    # The alloc_person_prop lesson: this app has been taken down by a Notion
    # column rename before. A project must fall out of enforcement, not 500.
    assert ops._budget_from_props({"Manthly budgt": {"number": 40}}) is None


# ---- month bounds ------------------------------------------------------

@check("month_bounds covers the whole calendar month")
def _():
    assert ops.month_bounds("2026-08-24") == ("2026-08-01", "2026-08-31")
    assert ops.month_bounds("2026-02-10") == ("2026-02-01", "2026-02-28")
    assert ops.month_bounds("2028-02-10") == ("2028-02-01", "2028-02-29")  # leap
    assert ops.month_bounds("2026-12-31") == ("2026-12-01", "2026-12-31")
    assert ops.month_bounds("2026-01-01") == ("2026-01-01", "2026-01-31")


# ---- the cap -----------------------------------------------------------

@check("Warn only never refuses, however far over")
def _():
    with fake_budget(budget(hours=10, policy=ops.POLICY_WARN), tracked=500):
        ops.check_budget("p", "2026-08-24", 8)  # must not raise


@check("an unbudgeted project never refuses")
def _():
    with fake_budget(None, tracked=500):
        ops.check_budget("p", "2026-08-24", 8)


@check("a hard cap refuses the hour that crosses it")
def _():
    with fake_budget(budget(hours=40, policy=ops.POLICY_BLOCK), tracked=38):
        ops.check_budget("p", "2026-08-24", 2)          # exactly 40: allowed
        try:
            ops.check_budget("p", "2026-08-24", 2.25)   # 40.25: refused
        except ops.BudgetExceeded as e:
            assert e.remaining == 2 and e.budget == 40
        else:
            raise AssertionError("should have been refused")


@check("an overrun % is respected")
def _():
    with fake_budget(budget(hours=40, policy=ops.POLICY_BLOCK, overrun=10), tracked=40):
        ops.check_budget("p", "2026-08-24", 4)          # 44 = the limit
        try:
            ops.check_budget("p", "2026-08-24", 4.5)
        except ops.BudgetExceeded:
            pass
        else:
            raise AssertionError("should have been refused past 110%")


@check("a write that LOWERS the total is never refused")
def _():
    # The rule that keeps an over-budget project fixable. Without it every
    # edit on an over-budget project - including the one that corrects it -
    # would be "over budget", and the project would be stuck for good.
    with fake_budget(budget(hours=40, policy=ops.POLICY_BLOCK), tracked=90):
        ops.check_budget("p", "2026-08-24", -10)
        ops.check_budget("p", "2026-08-24", 0)


@check("a 0 h budget refuses any hours at all")
def _():
    with fake_budget(budget(hours=0, policy=ops.POLICY_BLOCK), tracked=0):
        try:
            ops.check_budget("p", "2026-08-24", 0.25)
        except ops.BudgetExceeded:
            pass
        else:
            raise AssertionError("0 h means no hours allowed")


@check("floating point doesn't refuse an exactly-full budget")
def _():
    # 0.1+0.2 arithmetic: 7.5*3 lands a hair over 22.5 in binary floating
    # point, and a naive > would refuse the entry that exactly fills it.
    with fake_budget(budget(hours=22.5, policy=ops.POLICY_BLOCK), tracked=7.5 * 2):
        ops.check_budget("p", "2026-08-24", 7.5)


@check("the refusal message carries the numbers a person needs")
def _():
    with fake_budget(budget(hours=40, policy=ops.POLICY_BLOCK), tracked=39):
        try:
            ops.check_budget("p", "2026-08-24", 5)
        except ops.BudgetExceeded as e:
            msg = str(e)
            assert "40 h budget" in msg, msg
            assert "39 h are already logged" in msg, msg
            assert "1 h" in msg, msg
            assert "August 2026" in msg, msg


# ---- set_cell's delta --------------------------------------------------

class fake_cell:
    """Stand in for the Notion round trip set_cell makes."""

    def __init__(self, existing_hours):
        self.existing = existing_hours
        self.checked = []

    def __enter__(self):
        self._q, self._c, self._n = ops._query_all, ops.check_budget, ops._notion
        rows = [{"id": f"row{i}", "properties": {"Hours": {"number": h}}}
                for i, h in enumerate(self.existing)]
        ops._query_all = lambda kwargs: rows
        ops.check_budget = lambda pid, date, delta: self.checked.append(delta)

        class _N:
            class pages:
                @staticmethod
                def update(*a, **k): pass

                @staticmethod
                def create(*a, **k): pass
        ops._notion = _N
        self._ce = ops.create_entry
        ops.create_entry = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        ops._query_all, ops.check_budget, ops._notion = self._q, self._c, self._n
        ops.create_entry = self._ce


@check("set_cell checks the DELTA, not the submitted hours")
def _():
    # set_cell is an upsert: typing 3 into a cell that held 5 lowers the month
    # by 2. Checking the submitted 3 as if it were an addition would refuse
    # ordinary corrections all over a busy project.
    with fake_cell([5]) as f:
        ops.set_cell("person", "proj", "2026-08-24", 3, enforce=True)
        assert f.checked == [-2], f.checked


@check("set_cell's delta counts duplicate rows it is about to fold together")
def _():
    with fake_cell([3, 2]) as f:   # an old race left two rows: the cell is 5
        ops.set_cell("person", "proj", "2026-08-24", 6, enforce=True)
        assert f.checked == [1], f.checked


@check("a new cell adds all of its hours")
def _():
    with fake_cell([]) as f:
        ops.set_cell("person", "proj", "2026-08-24", 4, enforce=True)
        assert f.checked == [4], f.checked


@check("clearing a cell is a negative delta")
def _():
    with fake_cell([6]) as f:
        ops.set_cell("person", "proj", "2026-08-24", 0, enforce=True)
        assert f.checked == [-6], f.checked


@check("enforce=False skips the check entirely")
def _():
    # The default, so the CLIs and the Harvest importer are untouched.
    with fake_cell([5]) as f:
        ops.set_cell("person", "proj", "2026-08-24", 99)
        assert f.checked == [], f.checked


# ---- the status machine ------------------------------------------------

@check("statuses read the way the page claims")
def _():
    s = webapp._budget_status
    warn = budget(hours=40, policy=ops.POLICY_WARN)
    block = budget(hours=40, policy=ops.POLICY_BLOCK)
    over10 = budget(hours=40, policy=ops.POLICY_BLOCK, overrun=10)
    assert s(None, 12) == "none"
    assert s(warn, 0) == "ok"
    assert s(warn, 20) == "ok"
    assert s(warn, 38) == "warn"          # 95% of 40
    assert s(warn, 41) == "over"
    assert s(block, 40) == "blocked"      # sitting exactly on the cap
    assert s(block, 41) == "over_cap"     # only an admin or a CLI got here
    assert s(over10, 41) == "over"        # over budget, still under the cap
    assert s(over10, 45) == "over_cap"


@check("a fresh project with no hours is On track, not Warning")
def _():
    # 0 tracked satisfies "tracked >= warn threshold" whenever the budget is 0,
    # which would otherwise paint every empty project yellow. The `tracked > 0`
    # guard is what stops it.
    assert webapp._budget_status(budget(hours=0, policy=ops.POLICY_WARN), 0) == "ok"
    assert webapp._budget_status(budget(hours=40), 0) == "ok"


@check("a 0 h capped project reads as At the cap from the start")
def _():
    # Nothing can be logged to it, which is exactly what "At the cap" means.
    assert webapp._budget_status(budget(hours=0, policy=ops.POLICY_BLOCK), 0) == "blocked"


# ---- sorting -----------------------------------------------------------

@check("budgeted rows sort trouble-first, unbudgeted stay alphabetical below")
def _():
    projects = [
        {"id": "a", "name": "Alpha", "budget": None},
        {"id": "b", "name": "Bravo", "budget": budget(hours=10)},
        {"id": "c", "name": "Charlie", "budget": budget(hours=10)},
        {"id": "d", "name": "Delta", "budget": None},
    ]
    rows = webapp._budget_rows(projects, {"b": 2, "c": 40})
    assert [r["name"] for r in rows] == ["Charlie", "Bravo", "Alpha", "Delta"], \
        [r["name"] for r in rows]
    # the unbudgeted block keeps its stable alphabetical order, so typing a
    # column of numbers doesn't make the list jump under the cursor
    assert [r["name"] for r in rows[2:]] == ["Alpha", "Delta"]


@check("a project with no budget still reports its tracked hours")
def _():
    rows = webapp._budget_rows([{"id": "a", "name": "A", "budget": None}], {"a": 9})
    assert rows[0]["tracked"] == 9 and rows[0]["pct"] is None
    assert rows[0]["status"] == "none"


@check("a 0 h budget never yields a None percentage")
def _():
    # Regression: pct was None whenever hours was 0, and the template formats
    # it with %.0f — so one project budgeted at 0 took the whole page down
    # with a 500. A budget is a budget; only "unbudgeted" has no percentage.
    zero = budget(hours=0)
    for tracked, want in ((0, 0.0), (3, 100.0)):
        r = webapp._budget_rows([{"id": "a", "name": "A", "budget": zero}],
                                {"a": tracked})[0]
        assert r["pct"] == want, (tracked, r["pct"])
        assert isinstance(r["bar"], float) and 0 <= r["bar"] <= 100
        assert f"{r['pct']:.0f}"      # what the template does


@check("only an unbudgeted row has no percentage")
def _():
    r = webapp._budget_rows([{"id": "a", "name": "A", "budget": None}], {"a": 3})[0]
    assert r["pct"] is None and r["bar"] == 0


@check("the bar caps at 100% while the number keeps going")
def _():
    rows = webapp._budget_rows([{"id": "a", "name": "A", "budget": budget(hours=10)}],
                               {"a": 35})
    assert rows[0]["bar"] == 100 and round(rows[0]["pct"]) == 350


# ---- alerts ------------------------------------------------------------

class fake_alert:
    def __init__(self, b, tracked):
        self.b, self.tracked, self.written = b, tracked, []

    def __enter__(self):
        self._b, self._t, self._n, self._r = (
            ops.budget_for, ops.project_month_hours, ops._notion, ops.project_budgets)
        self._m = ops._project_name_map
        ops.budget_for = lambda pid: self.b
        ops.project_month_hours = lambda pid, date: self.tracked
        ops.project_budgets = lambda refresh=False: {}
        ops._project_name_map = lambda: {"p": "Test Project"}
        written = self.written

        class _N:
            class pages:
                @staticmethod
                def update(pid, properties=None, **k):
                    written.append(properties[ops.BUDGET_NOTIFIED_PROP]
                                   ["rich_text"][0]["text"]["content"])
        ops._notion = _N
        return self

    def __exit__(self, *exc):
        (ops.budget_for, ops.project_month_hours, ops._notion,
         ops.project_budgets) = self._b, self._t, self._n, self._r
        ops._project_name_map = self._m


@check("no alert below the warning threshold")
def _():
    with fake_alert(budget(hours=40), 30):
        assert ops.budget_alert("p", "2026-08-24") is None


@check("warn fires at the threshold, over fires at 100%")
def _():
    with fake_alert(budget(hours=40), 38) as f:
        a = ops.budget_alert("p", "2026-08-24")
        assert a["level"] == "warn" and f.written == ["2026-08:warn"]
    with fake_alert(budget(hours=40), 40) as f:
        assert ops.budget_alert("p", "2026-08-24")["level"] == "over"
        assert f.written == ["2026-08:over"]


@check("an alert fires once per project per month")
def _():
    # Without the stamp every later entry in an over-budget month sends
    # another email — the thing Harvest's over_budget_notification_date is for.
    with fake_alert(budget(hours=40, notified="2026-08:over"), 60) as f:
        assert ops.budget_alert("p", "2026-08-24") is None
        assert f.written == []


@check("having already warned doesn't suppress the over-budget alert")
def _():
    with fake_alert(budget(hours=40, notified="2026-08:warn"), 45) as f:
        assert ops.budget_alert("p", "2026-08-24")["level"] == "over"
        assert f.written == ["2026-08:over"]


@check("dropping back from over to warn does not re-alert")
def _():
    with fake_alert(budget(hours=40, notified="2026-08:over"), 38) as f:
        assert ops.budget_alert("p", "2026-08-24") is None
        assert f.written == []


@check("a new month alerts again")
def _():
    with fake_alert(budget(hours=40, notified="2026-07:over"), 45) as f:
        assert ops.budget_alert("p", "2026-08-01")["level"] == "over"
        assert f.written == ["2026-08:over"]


@check("an unbudgeted project never alerts")
def _():
    with fake_alert(None, 500):
        assert ops.budget_alert("p", "2026-08-24") is None


@check("a 0 h budget doesn't divide by zero")
def _():
    with fake_alert(budget(hours=0), 5):
        assert ops.budget_alert("p", "2026-08-24") is None


# ---- the switch --------------------------------------------------------

@check("budget alerts are off unless their own switch is on")
def _():
    from web import mailer
    old = os.environ.pop("BUDGET_ALERTS_ENABLED", None)
    try:
        assert mailer.budget_alerts_enabled() is False
        assert mailer.budget_transport() == ""
        os.environ["BUDGET_ALERTS_ENABLED"] = "1"
        assert mailer.budget_alerts_enabled() is True
    finally:
        os.environ.pop("BUDGET_ALERTS_ENABLED", None)
        if old is not None:
            os.environ["BUDGET_ALERTS_ENABLED"] = old


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
