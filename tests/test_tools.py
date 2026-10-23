#!/usr/bin/env python3
"""Tests for the /tools catalog.

Run:  ./.venv/bin/python tests/test_tools.py

Plain asserts and a tiny runner, like tests/test_people.py. **Nothing here
touches Notion**: the writes run against a fake client swapped in for
ops._notion, so no real Tools row is ever created or changed.
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


DS = "tools-ds-1"


class FakePages:
    def __init__(self, pages):
        self.pages, self.updates, self.created = pages, [], []

    def retrieve(self, page_id):
        return self.pages[page_id]

    def update(self, page_id, properties):
        self.updates.append((page_id, properties))

    def create(self, parent, properties):
        self.created.append(properties)
        return {"id": "new-1", "properties": {
            "Tool": {"title": [{"plain_text": properties["Tool"]["title"][0]["text"]["content"]}]},
            "Active": {"checkbox": True}}}


class FakeDS:
    def __init__(self, rows):
        self.rows = rows

    def query(self, **kw):
        return {"results": self.rows, "has_more": False}


class FakeNotion:
    def __init__(self, pages=None, rows=None):
        self.pages = FakePages(pages or {})
        self.data_sources = FakeDS(rows or [])


def tool_page(pid, name, people=(), parent=DS, active=True):
    return {"id": pid, "parent": {"data_source_id": parent}, "properties": {
        "Tool": {"title": [{"plain_text": name}]},
        "People": {"people": [{"object": "user", "id": i} for i in people]},
        "Active": {"checkbox": active}}}


def with_fake(fake, fn):
    saved = (ops._notion, ops.TOOLS_DS, ops.list_people)
    ops._notion, ops.TOOLS_DS = fake, DS
    ops.list_people = lambda: [{"id": "u-ana", "name": "Ana"}, {"id": "u-bo", "name": "Bo"}]
    try:
        return fn()
    finally:
        ops._notion, ops.TOOLS_DS, ops.list_people = saved


# ---- toggle_holders: the People array after a click -----------------------

@check("giving a tool appends the person")
def _():
    assert ops.toggle_holders(["a"], "b", True) == ["a", "b"]


@check("giving it twice doesn't duplicate (dash/case-blind)")
def _():
    assert ops.toggle_holders(["AB-CD"], "abcd", True) == ["abcd"]


@check("taking it away removes only that person, order kept")
def _():
    assert ops.toggle_holders(["a", "b", "c"], "b", False) == ["a", "c"]


# ---- assign_tool ------------------------------------------------------------

@check("assigning writes the full People array")
def _():
    fake = FakeNotion({"t1": tool_page("t1", "Slack", ["u-ana"])})
    out = with_fake(fake, lambda: ops.assign_tool("t1", "u-bo", True))
    assert out["count"] == 2
    assert fake.pages.updates == [("t1", {"People": {"people": [
        {"object": "user", "id": "u-ana"}, {"object": "user", "id": "u-bo"}]}})]


@check("an unchanged assignment makes no write")
def _():
    fake = FakeNotion({"t1": tool_page("t1", "Slack", ["u-ana"])})
    with_fake(fake, lambda: ops.assign_tool("t1", "u-ana", True))
    assert fake.pages.updates == []


@check("someone off the roster can't be given a tool, but can lose one")
def _():
    fake = FakeNotion({"t1": tool_page("t1", "Slack", ["u-gone"])})
    try:
        with_fake(fake, lambda: ops.assign_tool("t1", "u-gone", True))
        raise AssertionError("should refuse")
    except ValueError:
        pass
    with_fake(fake, lambda: ops.assign_tool("t1", "u-gone", False))
    assert fake.pages.updates == [("t1", {"People": {"people": []}})]


@check("a page from another database is refused")
def _():
    fake = FakeNotion({"p1": tool_page("p1", "Kepos", parent="projects-ds")})
    try:
        with_fake(fake, lambda: ops.assign_tool("p1", "u-ana", True))
        raise AssertionError("should refuse")
    except ValueError:
        pass
    assert fake.pages.updates == []


# ---- create / update --------------------------------------------------------

@check("a duplicate active name is refused, case/punctuation-blind")
def _():
    fake = FakeNotion(rows=[tool_page("t1", "Google Account")])
    try:
        with_fake(fake, lambda: ops.create_tool("google-account"))
        raise AssertionError("should refuse")
    except ValueError:
        pass
    assert fake.pages.created == []


@check("a retired tool's name can be reused")
def _():
    fake = FakeNotion(rows=[tool_page("t1", "Slack", active=False)])
    with_fake(fake, lambda: ops.create_tool("Slack"))
    assert len(fake.pages.created) == 1


@check("a bare domain gains https://")
def _():
    fake = FakeNotion()
    with_fake(fake, lambda: ops.create_tool("Claude", url="claude.ai"))
    assert fake.pages.created[0]["URL"] == {"url": "https://claude.ai"}


@check("an empty name is refused")
def _():
    try:
        with_fake(FakeNotion(), lambda: ops.create_tool("   "))
        raise AssertionError("should refuse")
    except ValueError:
        pass


@check("update refuses an unknown field before any Notion call")
def _():
    fake = FakeNotion({"t1": tool_page("t1", "Slack")})
    try:
        with_fake(fake, lambda: ops.update_tool("t1", "People", "x"))
        raise AssertionError("should refuse")
    except ValueError:
        pass
    assert fake.pages.updates == []


@check("retiring writes only Active")
def _():
    fake = FakeNotion({"t1": tool_page("t1", "Slack")})
    with_fake(fake, lambda: ops.update_tool("t1", "active", False))
    assert fake.pages.updates == [("t1", {"Active": {"checkbox": False}})]


@check("a renamed column reads as empty, not a crash")
def _():
    t = ops._tool_from_page({"id": "t1", "properties": {"Name": {"title": []}}})
    assert t["name"] == "(untitled)" and t["user_ids"] == [] and t["active"]


if __name__ == "__main__":
    if _FAILS:
        print("\n".join(_FAILS))
        sys.exit(1)
    print("all passed")
