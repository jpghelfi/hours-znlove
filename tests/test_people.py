#!/usr/bin/env python3
"""Tests for the /people roster editor.

Run:  ./.venv/bin/python tests/test_people.py

Plain asserts and a tiny runner, like tests/test_budgets.py. **Nothing here
touches Notion**: the pure rules (who the Add picker offers, the last-admin
guard) take plain lists, and the writes run against a fake client swapped in
for ops._notion, so no real People row is ever created or changed.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-for-anything")
os.environ["AUTH_DISABLED"] = "1"
os.environ["DEV_USER_ID"] = "u-admin"
os.environ["ADMIN_EMAILS"] = "dev@local"

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


def row(page_id, user_id, name="X", active=True, admin=False, approver=False):
    return {"page_id": page_id, "user_id": user_id, "name": name,
            "active": active, "admin": admin, "approver": approver}


# ---- add_candidates: who the picker offers -------------------------------

MEMBERS = [
    {"id": "aaaa-1111", "name": "Zoe", "email": "zoe@x"},
    {"id": "bbbb-2222", "name": "ana", "email": "ana@x"},
    {"id": "cccc-3333", "name": "Bo", "email": ""},
]


@check("a member with no row is offered as a new add")
def _():
    out = ops.add_candidates(MEMBERS, [])
    assert [c["name"] for c in out] == ["ana", "Bo", "Zoe"]   # sorted, case-insensitive
    assert all(c["page_id"] is None for c in out)


@check("a member with an active row is left out — matched by id, dashes or not")
def _():
    out = ops.add_candidates(MEMBERS, [row("r1", "AAAA1111")])
    assert "Zoe" not in [c["name"] for c in out]


@check("a member whose only row is inactive is offered for reactivation, not a new row")
def _():
    out = ops.add_candidates(MEMBERS, [row("r2", "bbbb2222", active=False)])
    ana = next(c for c in out if c["name"] == "ana")
    assert ana["page_id"] == "r2"


@check("an inactive duplicate doesn't resurrect someone who also has an active row")
def _():
    out = ops.add_candidates(MEMBERS, [row("r2", "bbbb-2222", active=False), row("r3", "bbbb-2222")])
    assert "ana" not in [c["name"] for c in out]


@check("rows with no linked user never hide a member")
def _():
    out = ops.add_candidates(MEMBERS, [row("r9", None, name="Bo")])
    assert "Bo" in [c["name"] for c in out]


# ---- check_last_admin ----------------------------------------------------

def refuses(roster, page_id, field, value):
    try:
        ops.check_last_admin(roster, page_id, field, value)
    except ops.LastAdmin:
        return True
    return False


@check("unticking Admin on the only active admin is refused")
def _():
    roster = [row("r1", "u1", admin=True), row("r2", "u2")]
    assert refuses(roster, "r1", "admin", False)


@check("unticking Active on the only active admin is refused too")
def _():
    roster = [row("r1", "u1", admin=True), row("r2", "u2")]
    assert refuses(roster, "r1", "active", False)


@check("with a second active admin, either untick goes through")
def _():
    roster = [row("r1", "u1", admin=True), row("r2", "u2", admin=True)]
    assert not refuses(roster, "r1", "admin", False)
    assert not refuses(roster, "r1", "active", False)


@check("an inactive admin or an unlinked admin row doesn't count as cover")
def _():
    roster = [row("r1", "u1", admin=True), row("r2", "u2", active=False, admin=True),
              row("r3", None, admin=True)]
    assert refuses(roster, "r1", "admin", False)


@check("a duplicate active admin row for the same person does count (access_ids would)")
def _():
    roster = [row("r1", "u1", admin=True), row("r2", "u-1", admin=True)]
    assert not refuses(roster, "r1", "admin", False)


@check("ticking, the approver box, and non-admin rows are never refused")
def _():
    roster = [row("r1", "u1", admin=True), row("r2", "u2")]
    assert not refuses(roster, "r1", "admin", True)
    assert not refuses(roster, "r1", "approver", False)
    assert not refuses(roster, "r2", "active", False)


# ---- writes, against a fake Notion ---------------------------------------

PEOPLE = "people-ds"


class FakeNotion:
    def __init__(self, rows):
        self.rows = {r["page_id"]: r for r in rows}
        self.updates, self.creates = [], []
        test = self

        class Pages:
            def retrieve(self, page_id):
                parent = PEOPLE if page_id in test.rows else "other-ds"
                return {"id": page_id, "parent": {"type": "data_source_id", "data_source_id": parent}}

            def update(self, page_id, properties):
                test.updates.append((page_id, properties))

            def create(self, parent, properties):
                test.creates.append((parent, properties))
                return {"id": "new-page"}

        self.pages = Pages()


class installed:
    def __init__(self, rows):
        self.fake = FakeNotion(rows)

    def __enter__(self):
        self.old = (ops._notion, ops.PEOPLE_DS, ops.list_roster, ops.list_workspace_members)
        ops._notion, ops.PEOPLE_DS = self.fake, PEOPLE
        ops.list_roster = lambda: list(self.fake.rows.values())
        ops.list_workspace_members = lambda: [dict(m) for m in MEMBERS]
        return self.fake

    def __exit__(self, *exc):
        ops._notion, ops.PEOPLE_DS, ops.list_roster, ops.list_workspace_members = self.old


def prime_cache():
    ops._access_cache.update(at=10 ** 12, allowed={"x"}, admins={"x"}, approvers=set())


@check("a flag write lands on the right property and drops the access cache")
def _():
    with installed([row("r1", "u1", admin=True), row("r2", "u2")]) as fake:
        prime_cache()
        ops.set_person_flag("r2", "approver", True)
        assert fake.updates == [("r2", {"Approves absences": {"checkbox": True}})]
        assert ops._access_cache["allowed"] is None


@check("a flag write on a page outside the People db is refused before writing")
def _():
    with installed([row("r1", "u1", admin=True)]) as fake:
        try:
            ops.set_person_flag("not-a-person", "active", False)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        assert fake.updates == []


@check("the last-admin guard holds on the real write path")
def _():
    with installed([row("r1", "u1", admin=True), row("r2", "u2")]) as fake:
        try:
            ops.set_person_flag("r1", "admin", False)
            raise AssertionError("expected LastAdmin")
        except ops.LastAdmin:
            pass
        assert fake.updates == []


@check("an unknown field is refused")
def _():
    with installed([row("r1", "u1")]) as fake:
        try:
            ops.set_person_flag("r1", "Name", True)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        assert fake.updates == []


@check("rename trims and collapses whitespace, refuses blank")
def _():
    with installed([row("r1", "u1")]) as fake:
        assert ops.rename_person("r1", "  Ana   María ")["name"] == "Ana María"
        assert fake.updates[0][1]["Name"]["title"][0]["text"]["content"] == "Ana María"
        try:
            ops.rename_person("r1", "   ")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


@check("add creates an Active row linked to the member")
def _():
    with installed([]) as fake:
        prime_cache()
        out = ops.add_person("cccc3333")
        assert out["page_id"] == "new-page"
        parent, props = fake.creates[0]
        assert parent["data_source_id"] == PEOPLE
        assert props["Person"]["people"][0]["id"] == "cccc-3333"
        assert props["Active"]["checkbox"] is True
        assert props["Name"]["title"][0]["text"]["content"] == "Bo"
        assert ops._access_cache["allowed"] is None


@check("add refuses someone who already has a row (inactive) and names the row")
def _():
    with installed([row("r2", "bbbb-2222", active=False)]) as fake:
        try:
            ops.add_person("bbbb-2222")
            raise AssertionError("expected AlreadyOnRoster")
        except ops.AlreadyOnRoster as exc:
            assert exc.page_id == "r2" and exc.active is False
        assert fake.creates == []


@check("add refuses an id that isn't a workspace member")
def _():
    with installed([]) as fake:
        try:
            ops.add_person("guest-or-bot")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        assert fake.creates == []


# ---- the page and its endpoints -----------------------------------------

def client():
    from fastapi.testclient import TestClient
    return TestClient(webapp.app)


@check("/people renders the roster, inactive rows included, and the picker")
def _():
    old = ops.access_ids
    ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": {"u-admin"}, "approvers": set()}
    try:
        with installed([row("r1", "u-admin", name="Dev", admin=True),
                        row("r2", "bbbb-2222", name="Ana Old", active=False)]):
            r = client().get("/people")
            assert r.status_code == 200, r.status_code
            assert "Dev" in r.text and "Ana Old" in r.text
            assert "Inactive — reactivate" in r.text and 'data-row="r2"' in r.text
            assert "Not on the roster" in r.text and "zoe@x" in r.text
    finally:
        ops.access_ids = old


@check("/people and its writes are admins only")
def _():
    old_ids, old_env = ops.access_ids, os.environ.pop("ADMIN_EMAILS", None)
    ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": set(), "approvers": set()}
    try:
        with installed([row("r1", "u1", admin=True)]) as fake:
            c = client()
            assert c.get("/people", follow_redirects=False).status_code == 303
            r = c.post("/api/people/flag", json={"page_id": "r1", "field": "admin", "value": False})
            assert r.status_code == 403
            assert c.post("/api/people/add", json={"user_id": "cccc-3333"}).status_code == 403
            assert fake.updates == [] and fake.creates == []
    finally:
        ops.access_ids = old_ids
        if old_env is not None:
            os.environ["ADMIN_EMAILS"] = old_env


@check("the last-admin refusal reaches the browser as a 409 with the reason")
def _():
    old = ops.access_ids
    ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": {"u-admin"}, "approvers": set()}
    try:
        with installed([row("r1", "u1", name="Dev", admin=True)]):
            r = client().post("/api/people/flag", json={"page_id": "r1", "field": "admin", "value": False})
            assert r.status_code == 409 and "last active admin" in r.json()["error"], (r.status_code, r.text)
    finally:
        ops.access_ids = old


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
