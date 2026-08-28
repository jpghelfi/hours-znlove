#!/usr/bin/env python3
"""Tests for goals — grouping a project's logged hours into what they went into.

Run:  ./.venv/bin/python tests/test_goals.py

Plain asserts and a tiny runner, like tests/test_budgets.py — the project has
no test dependency and this shouldn't be the change that adds one.

**Nothing here touches Notion.** The module-level client is built at import (so
a NOTION_TOKEN must exist in .env), but every function that would make a call
is monkeypatched. What's tested is the part a page load won't show you: the
tolerant parsing, the batch guard rails, the two target bases, and the rule
that Unassigned is always a row.
"""
from __future__ import annotations

import json
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


def goal(gid="g1", name="Maintenance", project="p1", target=None,
         basis="Total", status="Open"):
    return {"id": gid, "name": name, "project_id": project, "project": "P",
            "target": target, "basis": basis, "status": status,
            "started": None, "due": None, "note": ""}


def entry(eid, hours, goal_id=None, goal_name="", project="P", person="A"):
    return {"id": eid, "hours": hours, "goal_id": goal_id, "goal": goal_name,
            "project": project, "person": person, "date": "2026-08-10"}


class fake_goals:
    """Swap the goal list (and the entry relation column) for a with-block."""

    def __init__(self, goals, prop="Goal", ds="goals-ds"):
        self.goals, self.prop, self.ds = goals, prop, ds

    def __enter__(self):
        self._a, self._p, self._d = ops.all_goals, ops.goal_prop, ops.GOALS_DS
        self._t = ops.goal_totals
        ops.all_goals = lambda refresh=False: self.goals
        ops.goal_prop = lambda refresh=False: self.prop
        ops.goal_totals = lambda pid: {}
        ops.GOALS_DS = self.ds
        return self

    def __exit__(self, *exc):
        ops.all_goals, ops.goal_prop, ops.GOALS_DS = self._a, self._p, self._d
        ops.goal_totals = self._t


print("\ngoals\n")


# ---- parsing: a renamed or missing column must never raise ----------------

@check("a goal row reads defaults out of a half-empty page")
def _():
    page = {"id": "g1", "properties": {}}
    row = ops._goal_row(page, {})
    assert row["name"] == "(untitled)"
    assert row["project_id"] is None
    assert row["target"] is None       # empty is "untargeted", never 0
    assert row["basis"] == "Total"     # the default, not a crash
    assert row["status"] == "Open"


@check("a goal row reads a full page, and an unknown basis falls back to Total")
def _():
    page = {"id": "g1", "properties": {
        "Goal": {"title": [{"plain_text": "New homepage"}]},
        "Project": {"relation": [{"id": "p1"}]},
        "Target hours": {"number": 80},
        "Target basis": {"select": {"name": "Fortnightly"}},
        "Status": {"select": {"name": "Done"}},
        "Due": {"date": {"start": "2026-10-31"}},
    }}
    row = ops._goal_row(page, {"p1": "Fotosprint"})
    assert row["name"] == "New homepage"
    assert row["project"] == "Fotosprint"
    assert row["target"] == 80
    assert row["basis"] == "Total"
    assert row["status"] == "Done"
    assert row["due"] == "2026-10-31"


@check("target 0 survives as 0 — it is not the same as untargeted")
def _():
    page = {"id": "g", "properties": {"Target hours": {"number": 0}}}
    assert ops._goal_row(page, {})["target"] == 0


@check("an entry with no goal, or with the column missing, reads as unfiled")
def _():
    with fake_goals([goal()]):
        assert ops.entry_goal({})["goal_id"] is None
        assert ops.entry_goal({"Goal": {"relation": []}})["goal"] == ""
    with fake_goals([goal()], prop=None):
        assert ops.entry_goal({"Goal": {"relation": [{"id": "g1"}]}})["goal_id"] is None


@check("an entry pointing at a goal that no longer exists says so, doesn't crash")
def _():
    with fake_goals([goal(gid="g1")]):
        got = ops.entry_goal({"Goal": {"relation": [{"id": "gone"}]}})
        assert got["goal_id"] == "gone"
        assert got["goal"] == "(deleted goal)"


@check("the goal column is found under a renamed heading")
def _():
    # the lesson alloc_person_prop exists for: a relation knows what it points
    # at, so the *name* is never what this depends on
    calls = {}
    ops._goal_prop_cache.update({"at": 0.0, "name": None})
    old_ds, old_notion = ops.GOALS_DS, ops._notion

    class FakeNotion:
        class data_sources:
            @staticmethod
            def retrieve(ds):
                calls["ds"] = ds
                return {"properties": {
                    "Hours": {"type": "number"},
                    "Ticket": {"type": "relation", "relation": {"data_source_id": "other-ds"}},
                    "val": {"type": "relation", "relation": {"data_source_id": "goals-ds"}},
                }}
    try:
        ops.GOALS_DS, ops._notion = "goals-ds", FakeNotion
        assert ops.goal_prop(refresh=True) == "val"
    finally:
        ops.GOALS_DS, ops._notion = old_ds, old_notion
        ops._goal_prop_cache.update({"at": 0.0, "name": None})


@check("no relation pointing at Goals reads as 'not set up', not an error")
def _():
    old_ds, old_notion = ops.GOALS_DS, ops._notion

    class FakeNotion:
        class data_sources:
            @staticmethod
            def retrieve(ds):
                return {"properties": {"Hours": {"type": "number"}}}
    try:
        ops.GOALS_DS, ops._notion = "goals-ds", FakeNotion
        assert ops.goal_prop(refresh=True) is None
    finally:
        ops.GOALS_DS, ops._notion = old_ds, old_notion
        ops._goal_prop_cache.update({"at": 0.0, "name": None})


# ---- the create picker's suggestions --------------------------------------

@check("names used on other projects are offered once, counted, and normalised")
def _():
    goals = [
        goal("a", "Maintenance", "p1"),
        goal("b", "maintenance", "p2"),      # same name, different spelling
        goal("c", "Maintenance", "p3"),
        goal("d", "New homepage", "p2"),
        goal("e", "Old thing", "p2", status="Done"),
    ]
    with fake_goals(goals):
        out = ops.other_project_goal_names("p1")
        names = {r["name"].lower(): r["count"] for r in out}
        # p1 already has Maintenance, so it isn't suggested back to itself
        assert "maintenance" not in names, out
        assert names["new homepage"] == 1
        # a closed goal is not a spelling anyone should adopt
        assert "old thing" not in names
        out2 = ops.other_project_goal_names("p9")
        by = {r["name"].lower(): r["count"] for r in out2}
        assert by["maintenance"] == 3      # three projects, one row


# ---- filing entries -------------------------------------------------------

class FakePages:
    def __init__(self, fail: set | None = None):
        self.updated: list = []
        self.fail = fail or set()

    def update(self, page_id, **kwargs):
        if page_id in self.fail:
            raise RuntimeError("nope")
        self.updated.append((page_id, kwargs))


class fake_writes:
    def __init__(self, fail=None):
        self.pages = FakePages(fail)

    def __enter__(self):
        self._n = ops._notion

        class FakeNotion:
            pages = self.pages
        ops._notion = FakeNotion
        return self.pages

    def __exit__(self, *exc):
        ops._notion = self._n


@check("filing writes only the goal property, once per entry")
def _():
    with fake_goals([goal("g1")]), fake_writes() as pages:
        res = ops.set_entry_goals(["e1", "e2"], "g1", allowed_ids={"e1", "e2"})
    assert res == {"ok": True, "updated": 2, "failed": []}
    assert len(pages.updated) == 2
    # exactly one property, so nothing else on the entry is clobbered
    props = pages.updated[0][1]["properties"]
    assert list(props) == ["Goal"]
    assert props["Goal"] == {"relation": [{"id": "g1"}]}


@check("clearing a goal writes an empty relation, not a missing one")
def _():
    with fake_goals([goal("g1")]), fake_writes() as pages:
        ops.set_entry_goals(["e1"], None, allowed_ids={"e1"})
    assert pages.updated[0][1]["properties"]["Goal"] == {"relation": []}


@check("an entry outside the project and period on screen is refused, whole")
def _():
    with fake_goals([goal("g1")]), fake_writes() as pages:
        try:
            ops.set_entry_goals(["e1", "sneaky"], "g1", allowed_ids={"e1"})
            raise AssertionError("should have refused")
        except ValueError as exc:
            assert "not in this project" in str(exc)
    # refused before the first write, so no half-applied batch
    assert pages.updated == []


@check("ids are compared with and without dashes")
def _():
    dashed = "3c901234-695c-81a4-a82f-f804670027e9"
    bare = dashed.replace("-", "")
    with fake_goals([goal("g1")]), fake_writes() as pages:
        ops.set_entry_goals([dashed], "g1", allowed_ids={bare})
    assert len(pages.updated) == 1


@check("a batch bigger than the cap is refused before anything is written")
def _():
    with fake_goals([goal("g1")]), fake_writes() as pages:
        try:
            ops.set_entry_goals([f"e{i}" for i in range(ops.MAX_GOAL_ASSIGN + 1)], "g1")
            raise AssertionError("should have refused")
        except ValueError as exc:
            assert "too many" in str(exc)
    assert pages.updated == []


@check("one entry that fails doesn't lose the rest of the batch")
def _():
    with fake_goals([goal("g1")]), fake_writes(fail={"e2"}) as pages:
        res = ops.set_entry_goals(["e1", "e2", "e3"], "g1", allowed_ids={"e1", "e2", "e3"})
    assert res["updated"] == 2
    assert res["failed"] == ["e2"]
    assert res["ok"] is False          # the caller has to be able to say so


@check("duplicate ids are filed once")
def _():
    with fake_goals([goal("g1")]), fake_writes() as pages:
        res = ops.set_entry_goals(["e1", "e1", "e2"], "g1", allowed_ids={"e1", "e2"})
    assert res["updated"] == 2 and len(pages.updated) == 2


@check("filing under a goal that doesn't exist is refused")
def _():
    with fake_goals([goal("g1")]), fake_writes() as pages:
        try:
            ops.set_entry_goals(["e1"], "nope", allowed_ids={"e1"})
            raise AssertionError("should have refused")
        except ValueError as exc:
            assert "unknown goal" in str(exc)
    assert pages.updated == []


@check("a goal from another project is refused — entries can't be filed across")
def _():
    # not reachable from the UI, but the goal id comes from the browser. A
    # mis-filed entry would go missing from both projects' blocks while still
    # counting toward the total, which is the one thing the block promises.
    goals = [goal("g1", "Theirs", project="p2")]
    with fake_goals(goals), fake_writes() as pages:
        try:
            ops.set_entry_goals(["e1"], "g1", allowed_ids={"e1"}, project_id="p1")
            raise AssertionError("should have refused")
        except ValueError as exc:
            assert "another project" in str(exc)
    assert pages.updated == []


@check("a goal from the same project is filed normally")
def _():
    goals = [goal("g1", "Ours", project="p1")]
    with fake_goals(goals), fake_writes() as pages:
        res = ops.set_entry_goals(["e1"], "g1", allowed_ids={"e1"}, project_id="p1")
    assert res["updated"] == 1 and len(pages.updated) == 1


@check("clearing a goal needs no project — there is no goal to belong anywhere")
def _():
    with fake_goals([goal("g1", project="p2")]), fake_writes() as pages:
        res = ops.set_entry_goals(["e1"], None, allowed_ids={"e1"}, project_id="p1")
    assert res["updated"] == 1


@check("filing when goals aren't set up is refused, not silently skipped")
def _():
    with fake_goals([], prop=None):
        try:
            ops.set_entry_goals(["e1"], "g1")
            raise AssertionError("should have refused")
        except ValueError as exc:
            assert "not set up" in str(exc)


# ---- deleting a goal ------------------------------------------------------

class fake_query:
    """Answer the Time Entries query with a fixed number of rows, paged."""

    def __init__(self, total, page=100):
        self.total, self.page = total, page
        self.asked = []

    def __enter__(self):
        outer = self

        class FakeNotion:
            class data_sources:
                @staticmethod
                def query(**kw):
                    outer.asked.append(kw)
                    start = int(kw.get("start_cursor") or 0)
                    n = min(outer.page, max(0, outer.total - start))
                    more = start + n < outer.total
                    return {"results": [{"id": f"e{start + i}"} for i in range(n)],
                            "has_more": more, "next_cursor": str(start + n)}

            class pages:
                @staticmethod
                def retrieve(pid):
                    return {"id": pid, "parent": {"data_source_id": "goals-ds"}}

                @staticmethod
                def update(pid, **kw):
                    outer.archived = (pid, kw)
        self._n = ops._notion
        ops._notion = FakeNotion
        self.archived = None
        return self

    def __exit__(self, *exc):
        ops._notion = self._n


@check("a goal with nothing filed under it is deleted")
def _():
    with fake_goals([goal("g1")]), fake_query(0) as q:
        res = ops.delete_goal("g1")
    assert res["deleted"] is True
    assert q.archived[0] == "g1" and q.archived[1]["archived"] is True


@check("a goal with hours filed under it is refused, and says how many")
def _():
    with fake_goals([goal("g1")]), fake_query(12) as q:
        try:
            ops.delete_goal("g1")
            raise AssertionError("should have refused")
        except ops.GoalInUse as exc:
            assert exc.count == 12
            assert "12 entries are still filed" in str(exc)
    # nothing archived: the refusal happens before the write
    assert q.archived is None


@check("one entry still filed reads as singular, and still refuses")
def _():
    with fake_goals([goal("g1")]), fake_query(1) as q:
        try:
            ops.delete_goal("g1")
            raise AssertionError("should have refused")
        except ops.GoalInUse as exc:
            assert exc.count == 1 and "1 entry is still filed" in str(exc)
    assert q.archived is None


@check("the count is over all time, not just a period")
def _():
    # an old entry pointing at a deleted goal is exactly the damage this
    # prevents, so the query carries no date bound at all
    with fake_goals([goal("g1")]), fake_query(3) as q:
        try:
            ops.delete_goal("g1")
        except ops.GoalInUse:
            pass
    f = q.asked[0]["filter"]
    assert "relation" in f and f["property"] == "Goal", f
    assert "date" not in json.dumps(f).lower()


@check("counting pages, and stops at the cap instead of walking thousands")
def _():
    with fake_goals([goal("g1")]), fake_query(250):
        assert ops.goal_entry_count("g1") == (250, False)
    with fake_goals([goal("g1")]), fake_query(5000):
        n, more = ops.goal_entry_count("g1", cap=500)
        assert n == 500 and more is True


@check("deleting checks the page is one of ours before counting anything")
def _():
    with fake_goals([goal("g1")]):
        old = ops._notion

        class FakeNotion:
            class pages:
                @staticmethod
                def retrieve(pid):
                    return {"id": pid, "parent": {"data_source_id": "some-other-db"}}
        try:
            ops._notion = FakeNotion
            try:
                ops.delete_goal("not-ours")
                raise AssertionError("should have refused")
            except ValueError as exc:
                assert "not a goal" in str(exc)
        finally:
            ops._notion = old


@check("deleting is refused outright when the Goal column can't be read")
def _():
    # Everywhere else an unresolvable column degrades to "no goals" and a page
    # renders without them. Here that same silence would read as "nothing is
    # filed under this goal" — and this relation is named `val` in Notion
    # today, so the column being unreadable is not hypothetical.
    with fake_goals([goal("g1")], prop=None), fake_query(500) as q:
        try:
            ops.delete_goal("g1")
            raise AssertionError("should have refused")
        except ops.GoalInUse:
            raise AssertionError("should not have reported a count at all")
        except ValueError as exc:
            assert "can't be read" in str(exc), exc
    assert q.archived is None


@check("counting is refused too, rather than answering zero")
def _():
    with fake_goals([goal("g1")], prop=None):
        try:
            ops.goal_entry_count("g1")
            raise AssertionError("should have refused")
        except ValueError:
            pass


@check("the count and the archive happen under the write lock together")
def _():
    # otherwise an entry filed between the two is stranded by a delete that had
    # already decided the goal was empty
    import threading
    held = []
    with fake_goals([goal("g1")]), fake_query(0) as q:
        real = ops._notion.pages.update

        def watching(pid, **kw):
            held.append(ops._write_lock.locked())
            return real(pid, **kw)
        ops._notion.pages.update = watching
        ops.delete_goal("g1")
    assert held == [True], held
    assert not ops._write_lock.locked()   # and it is given back


@check("deleting when goals aren't set up is refused")
def _():
    with fake_goals([], prop=None, ds=None):
        try:
            ops.delete_goal("g1")
            raise AssertionError("should have refused")
        except ValueError as exc:
            assert "not set up" in str(exc)


# ---- the goals block ------------------------------------------------------

@check("Unassigned is always the last row, even when everything is filed")
def _():
    goals = [goal("g1", "Maintenance")]
    entries = [entry("e1", 4, "g1", "Maintenance")]
    with fake_goals(goals):
        rows = webapp._goal_rows("p1", entries, goals, "monthly")
    assert rows[-1]["name"] == "Unassigned"
    assert rows[-1]["hours"] == 0
    assert rows[-1]["unassigned"] is True


@check("shares are of the period's total, and they include the unfiled hours")
def _():
    goals = [goal("g1", "A"), goal("g2", "B")]
    entries = [entry("e1", 5, "g1", "A"), entry("e2", 3, "g2", "B"), entry("e3", 2)]
    with fake_goals(goals):
        rows = webapp._goal_rows("p1", entries, goals, "monthly")
    by = {r["name"]: r for r in rows}
    assert by["A"]["hours"] == 5 and by["A"]["share"] == 50
    assert by["B"]["share"] == 30
    assert by["Unassigned"]["hours"] == 2 and by["Unassigned"]["share"] == 20
    assert sum(r["share"] for r in rows) == 100


@check("an open goal with nothing logged still shows; a closed empty one doesn't")
def _():
    goals = [goal("g1", "Open one"), goal("g2", "Finished", status="Done")]
    with fake_goals(goals):
        rows = webapp._goal_rows("p1", [], goals, "monthly")
    names = [r["name"] for r in rows]
    assert "Open one" in names
    assert "Finished" not in names      # history, not this period's business


@check("a closed goal with hours in the period is still shown")
def _():
    goals = [goal("g2", "Finished", status="Done")]
    entries = [entry("e1", 3, "g2", "Finished")]
    with fake_goals(goals):
        rows = webapp._goal_rows("p1", entries, goals, "monthly")
    assert [r["name"] for r in rows][0] == "Finished"


@check("a 'per month' target measures the month; a 'total' one measures the life")
def _():
    goals = [goal("g1", "Maintenance", target=10, basis="Per month"),
             goal("g2", "Homepage", target=80, basis="Total")]
    entries = [entry("e1", 4, "g1", "Maintenance"), entry("e2", 6, "g2", "Homepage")]
    with fake_goals(goals):
        ops.goal_totals = lambda pid: {"g2": 62}     # 62 h over its whole life
        rows = webapp._goal_rows("p1", entries, goals, "monthly")
    by = {r["name"]: r for r in rows}
    assert by["Maintenance"]["meter"]["used"] == 4      # this month only
    assert by["Maintenance"]["meter"]["scope"] == "this month"
    assert by["Homepage"]["meter"]["used"] == 62       # not the 6 logged this month
    assert by["Homepage"]["meter"]["scope"] == "all time"


@check("a target over its number reads as over")
def _():
    goals = [goal("g1", "Maintenance", target=4, basis="Per month")]
    entries = [entry("e1", 6, "g1", "Maintenance")]
    with fake_goals(goals):
        rows = webapp._goal_rows("p1", entries, goals, "monthly")
    m = rows[0]["meter"]
    assert m["over"] is True and m["pct"] == 150


@check("no meter off a monthly period — a per-month target on a Tuesday is nonsense")
def _():
    goals = [goal("g1", "Maintenance", target=10, basis="Per month")]
    entries = [entry("e1", 4, "g1", "Maintenance")]
    with fake_goals(goals):
        for period in ("weekly", "daily"):
            rows = webapp._goal_rows("p1", entries, goals, period)
            assert rows[0]["meter"] is None, period


@check("an untargeted goal gets no meter at all")
def _():
    goals = [goal("g1", "Maintenance")]
    with fake_goals(goals):
        rows = webapp._goal_rows("p1", [entry("e1", 4, "g1", "Maintenance")], goals, "monthly")
    assert rows[0]["meter"] is None


@check("a 0 h target still gets a meter — 0 is not the same as untargeted")
def _():
    # the empty-vs-0 trap the budgets docs spell out: empty means "no target",
    # 0 means "no hours are supposed to go here at all"
    goals = [goal("g1", "Should be idle", target=0, basis="Per month")]
    with fake_goals(goals):
        rows = webapp._goal_rows("p1", [], goals, "monthly")
        assert rows[0]["meter"] is not None
        assert rows[0]["meter"]["pct"] == 0 and rows[0]["meter"]["over"] is False
        # anything logged against it is instantly over
        rows = webapp._goal_rows("p1", [entry("e1", 2, "g1", "Should be idle")],
                                 goals, "monthly")
        assert rows[0]["meter"]["over"] is True
        assert rows[0]["meter"]["pct"] == 100      # not a division by zero


@check("a stale ?goal= degrades to the whole project, and 'none' is honoured")
def _():
    goals = [goal("g1")]
    assert webapp._goal_pick("g1", goals) == "g1"
    assert webapp._goal_pick("gone", goals) is None
    assert webapp._goal_pick("none", goals) == "none"
    assert webapp._goal_pick(None, goals) is None


# ---- the cross-project report --------------------------------------------

@check("the by-goal report groups one name across projects")
def _():
    entries = [
        entry("e1", 4, "g1", "Maintenance", project="Fotosprint"),
        entry("e2", 6, "g2", "maintenance", project="CaliforniaBorn"),
        entry("e3", 2, "g3", "New homepage", project="Fotosprint"),
    ]
    rows = webapp._by_goal(entries)
    by = {r["name"].lower(): r for r in rows}
    assert by["maintenance"]["hours"] == 10        # one row, two spellings
    assert by["maintenance"]["projects"] == 2
    assert by["maintenance"]["where"] == "2 projects"
    # one project is worth naming outright
    assert by["new homepage"]["where"] == "Fotosprint"


@check("unfiled hours are a row in the report, and they sort last")
def _():
    entries = [entry("e1", 1, "g1", "Maintenance"), entry("e2", 500)]
    rows = webapp._by_goal(entries)
    assert rows[-1]["name"] == "Unassigned"
    assert rows[-1]["hours"] == 500                # last, but not hidden
    assert rows[-1]["unassigned"] is True
    assert rows[0]["name"] == "Maintenance"


@check("an empty range makes an empty report, not a lone Unassigned row")
def _():
    assert webapp._by_goal([]) == []


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
