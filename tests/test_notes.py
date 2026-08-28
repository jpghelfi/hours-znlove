#!/usr/bin/env python3
"""Tests for "say what you worked on" — a description or a linked ticket.

Run:  ./.venv/bin/python tests/test_notes.py

Plain asserts and a tiny runner, like tests/test_budgets.py and
tests/test_goals.py — the project has no test dependency.

**Nothing here touches Notion.** What's tested is the part a page load won't
show you: which write paths the rule binds, which it deliberately doesn't, and
that a cell correcting an existing number is never asked to justify itself.
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


class fake_writes:
    """Capture what would have been written, and fake the reads around it."""

    def __init__(self, existing: list | None = None):
        self.existing = existing or []

    def __enter__(self):
        outer = self
        self.created: list = []
        self.updated: list = []

        class FakeNotion:
            class pages:
                @staticmethod
                def create(**kw):
                    outer.created.append(kw)
                    return {"id": "new"}

                @staticmethod
                def update(pid, **kw):
                    outer.updated.append((pid, kw))

            class data_sources:
                @staticmethod
                def query(**kw):
                    return {"results": outer.existing, "has_more": False}
        self._n, self._m = ops._notion, ops._project_name_map
        ops._notion = FakeNotion
        ops._project_name_map = lambda: {"p": "Test Project"}
        return self

    def __exit__(self, *exc):
        ops._notion, ops._project_name_map = self._n, self._m


def row(hours=3.0, pid="row1"):
    return {"id": pid, "properties": {"Hours": {"number": hours}}}


print("\nsay what you worked on\n")


# ---- the rule itself ------------------------------------------------------

@check("the floor is exactly MIN_DESCRIPTION characters, and one short fails")
def _():
    assert ops.note_ok("a" * ops.MIN_DESCRIPTION) is True
    assert ops.note_ok("a" * (ops.MIN_DESCRIPTION - 1)) is False


@check("a ticket satisfies it on its own, with no description at all")
def _():
    assert ops.note_ok("", "https://www.notion.so/Some-ticket-abc123") is True
    assert ops.note_ok("ZN-999", "https://www.notion.so/x-abc123") is True


@check("whitespace doesn't count — an entry described in spaces is blank")
def _():
    assert ops.note_ok(" " * 40) is False
    assert ops.note_ok("\n\t  \n") is False
    # and it isn't counted twice inside a real sentence either: five spaces
    # between two letters is two characters of description, not seven
    assert ops.note_ok("a     b") is False            # 7 raw, 3 real
    assert ops.note_ok("fixed the cart") is True


@check("the short descriptions that name the work are kept")
def _():
    # the reason the floor is five and not ten: these were measured in the
    # real data, and every one of them says more than a padded sentence would
    for text in ("ZN-999", "TB-54", "PR Review", "banners", "Deploy", "Rebuy"):
        assert ops.note_ok(text) is True, text
    # while the ones that say nothing still don't
    for text in ("pm", "x", "", "  "):
        assert ops.note_ok(text) is False, text


@check("nothing at all is refused")
def _():
    assert ops.note_ok() is False
    assert ops.note_ok("", "") is False


@check("the refusal names both ways out")
def _():
    try:
        ops.require_note("nope")
        raise AssertionError("should have refused")
    except ops.NoteRequired as exc:
        text = str(exc)
        assert str(ops.MIN_DESCRIPTION) in text
        assert "ticket" in text.lower()


# ---- which write paths it binds -------------------------------------------

@check("create_entry lets everything through until it is asked not to")
def _():
    # the default has to stay off: sync_harvest writes "Harvest" as its whole
    # description, and refusing an import corrupts the record to protect a rule
    with fake_writes() as w:
        ops.create_entry("u", "p", "2026-08-10", 3, "Harvest")
    assert len(w.created) == 1


@check("create_entry refuses a bare entry once it is")
def _():
    with fake_writes() as w:
        try:
            ops.create_entry("u", "p", "2026-08-10", 3, "", note=True)
            raise AssertionError("should have refused")
        except ops.NoteRequired:
            pass
    assert w.created == []      # refused before the write, not after


@check("create_entry accepts a ticket in place of a description")
def _():
    with fake_writes() as w:
        ops.create_entry("u", "p", "2026-08-10", 3, "",
                         task_url="https://www.notion.so/T-abc123", note=True)
    assert len(w.created) == 1


@check("the check runs before the Notion read, not after")
def _():
    # _project_name_map is a round trip; a refusal shouldn't pay for it
    called = []
    with fake_writes():
        ops._project_name_map = lambda: called.append(1) or {}
        try:
            ops.create_entry("u", "p", "2026-08-10", 3, "", note=True)
        except ops.NoteRequired:
            pass
    assert called == []


# ---- the weekly grid ------------------------------------------------------

@check("a new cell has to say what it is")
def _():
    with fake_writes(existing=[]) as w:
        try:
            ops.set_cell("u", "p", "2026-08-10", 3, note=True)
            raise AssertionError("should have refused")
        except ops.NoteRequired:
            pass
    assert w.created == [] and w.updated == []


@check("a new cell with a description is written, description and all")
def _():
    with fake_writes(existing=[]) as w:
        ops.set_cell("u", "p", "2026-08-10", 3, note=True,
                     description="rebuilt the checkout page")
    assert len(w.created) == 1
    props = w.created[0]["properties"]
    text = props["Description"]["rich_text"][0]["text"]["content"]
    assert text == "rebuilt the checkout page"


@check("correcting a cell that already holds hours asks for nothing")
def _():
    # the entry exists and keeps whatever it says; this write only moves the
    # number, and an entry logged before the rule has to stay fixable
    with fake_writes(existing=[row(hours=3)]) as w:
        res = ops.set_cell("u", "p", "2026-08-10", 5, note=True)
    assert res["hours"] == 5
    assert w.created == [] and len(w.updated) == 1
    # and the update leaves the description alone
    assert set(w.updated[0][1]["properties"]) == {"Hours", "Person"}


@check("blanking a cell to delete it asks for nothing")
def _():
    with fake_writes(existing=[row(hours=3)]) as w:
        res = ops.set_cell("u", "p", "2026-08-10", 0, note=True)
    assert res["hours"] == 0
    assert w.updated[0][1]["archived"] is True


@check("a new cell can be justified by a ticket instead")
def _():
    with fake_writes(existing=[]) as w:
        ops.set_cell("u", "p", "2026-08-10", 3, note=True,
                     task_url="https://www.notion.so/T-abc123", task_label="ZN-999")
    assert len(w.created) == 1
    props = w.created[0]["properties"]
    assert props["Task URL"]["url"].endswith("abc123")
    assert props["Task"]["rich_text"][0]["text"]["content"] == "ZN-999"


@check("the description is asked for before the budget, on the grid too")
def _():
    # create_entry checks the note first on purpose — when a new cell is both
    # undescribed and over budget, the description is the one the person can
    # act on, and two refusals for one save is one too many
    seen = []
    real = ops.check_budget
    with fake_writes(existing=[]) as w:
        ops.check_budget = lambda pid, date, delta: seen.append("budget")
        try:
            ops.set_cell("u", "p", "2026-08-10", 3, enforce=True, note=True)
            raise AssertionError("should have refused")
        except ops.NoteRequired:
            pass
        finally:
            ops.check_budget = real
    assert seen == [], "the budget was consulted before the description"
    assert w.created == []


@check("a described cell still gets its budget checked")
def _():
    seen = []
    real = ops.check_budget
    with fake_writes(existing=[]) as w:
        ops.check_budget = lambda pid, date, delta: seen.append(delta)
        try:
            ops.set_cell("u", "p", "2026-08-10", 3, enforce=True, note=True,
                         description="rebuilt the checkout page")
        finally:
            ops.check_budget = real
    assert seen == [3], seen
    assert len(w.created) == 1


@check("the grid is unaffected until it is asked to enforce")
def _():
    with fake_writes(existing=[]) as w:
        ops.set_cell("u", "p", "2026-08-10", 3)
    assert len(w.created) == 1


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
