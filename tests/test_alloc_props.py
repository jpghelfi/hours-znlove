#!/usr/bin/env python3
"""Tests for resolving the Allocations columns by schema.

Run:  ./.venv/bin/python tests/test_alloc_props.py

Plain asserts and a tiny runner, like tests/test_budgets.py. **Nothing here
touches Notion**: the schema read and the page create are stubbed. The bug this
guards: the title column was renamed `Allocation` → `Allocations` in Notion and
every new booking failed, because the create wrote the title by name.
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


def reset():
    ops._alloc_person_cache.update(at=0.0, name=None, title=None)


def stub_schema(props=None, fail=False):
    def retrieve(**_kw):
        if fail:
            raise RuntimeError("notion is down")
        return {"properties": props}
    ops._notion.data_sources.retrieve = retrieve


SCHEMA = {
    "Allocations": {"type": "title"},
    "Person": {"type": "people"},
    "Project": {"type": "relation"},
    "Week": {"type": "date"},
    "Hours": {"type": "number"},
}


@check("a renamed title column is written under its new name")
def _():
    reset()
    stub_schema(SCHEMA)
    created = {}
    ops._query_all = lambda kwargs: []                      # no existing booking
    ops._project_name_map = lambda: {"p1": "Kepos"}
    ops._notion.pages.create = lambda **kw: created.update(kw)
    ops._set_allocation_locked("u1", "p1", "2026-10-26", 4)
    props = created["properties"]
    assert "Allocations" in props and "Allocation" not in props
    assert props["Person"] == {"people": [{"id": "u1"}]}


@check("a renamed people column is still found")
def _():
    reset()
    stub_schema({**{k: v for k, v in SCHEMA.items() if k != "Person"},
                 "Who": {"type": "people"}})
    assert ops.alloc_person_prop() == "Who"
    assert ops.alloc_title_prop() == "Allocations"


@check("a failed schema read falls back and isn't cached")
def _():
    reset()
    stub_schema(fail=True)
    assert ops.alloc_title_prop() == "Allocation"
    assert ops.alloc_person_prop() == "Person"
    stub_schema(SCHEMA)
    assert ops.alloc_title_prop() == "Allocations"   # next call reads again


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
