#!/usr/bin/env python3
"""Tests for planning: the timeline geometry, the share strip, the item
validation, tracked-hours resolution, the share token, the PDF, and the two
pages rendered end to end against a fake Notion.

Run:  ./.venv/bin/python tests/test_plan.py

Plain asserts and a tiny runner, no pytest (the project has no test
dependency). **Nothing here touches Notion**: every read and write in
notion_ops is monkeypatched. The strip is the test that matters most —
a bug there is a leak to whoever holds a share link.
"""
from __future__ import annotations

import datetime as dt
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
from web import plan_pdf  # noqa: E402

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


def item(**kw):
    base = {"id": "i1", "name": "Homepage", "project_id": "p1", "project": "Acme",
            "start": "2026-09-08", "end": "2026-09-10", "status": "Planned", "type": "Feature",
            "estimate": 10.0, "owner_id": "u2", "owner": "Ana", "goal_id": None, "goal": "",
            "ticket_url": "", "ticket": "", "note": "secret", "internal": False, "order": 0}
    base.update(kw)
    return base


RNG = webapp._period_range("weekly", dt.date(2026, 9, 9))       # Mon 7 – Sun 13 Sep 2026
COLS = webapp._plan_columns(RNG)


# ---- geometry ------------------------------------------------------------

@check("a week has five weekday columns, Monday first")
def _():
    assert [c["dow"] for c in COLS] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert COLS[0]["date"] == "2026-09-07"


@check("a bar inside the week gets 1-based inclusive columns")
def _():
    rows = webapp._plan_period_rows([item()], RNG, COLS)
    assert rows[0]["col_a"] == 2 and rows[0]["col_b"] == 4
    assert not rows[0]["clip_l"] and not rows[0]["clip_r"]


@check("a bar running past both edges is clipped, not dropped")
def _():
    rows = webapp._plan_period_rows([item(start="2026-09-01", end="2026-09-30")], RNG, COLS)
    assert rows[0]["col_a"] == 1 and rows[0]["col_b"] == 5
    assert rows[0]["clip_l"] and rows[0]["clip_r"]


@check("an item entirely outside the period is left out; an unscheduled one is kept")
def _():
    rows = webapp._plan_period_rows([item(id="a", start="2026-08-03", end="2026-08-04"),
                                     item(id="b", start=None, end=None)], RNG, COLS)
    assert [r["id"] for r in rows] == ["b"]
    assert rows[0]["col_a"] is None


@check("a weekend-only item has no column to sit on and is left out")
def _():
    rows = webapp._plan_period_rows([item(start="2026-09-12", end="2026-09-13")], RNG, COLS)
    assert rows == []


@check("done and dropped rows sink below live ones, then by start date")
def _():
    rows = webapp._plan_period_rows([
        item(id="d", status="Done", start="2026-09-07", end="2026-09-07"),
        item(id="b", start="2026-09-09", end="2026-09-09"),
        item(id="a", start="2026-09-08", end="2026-09-08"),
        item(id="x", status="Dropped", start="2026-09-07", end="2026-09-07"),
    ], RNG, COLS)
    assert [r["id"] for r in rows] == ["a", "b", "d", "x"]


@check("the month view has ~22 weekday columns and marks the 1st with its month")
def _():
    rng = webapp._period_range("monthly", dt.date(2026, 9, 1))
    cols = webapp._plan_columns(rng)
    assert len(cols) == 22
    assert cols[0]["month"] == "Sep"


# ---- the strip -------------------------------------------------------------

@check("the strip drops internal items and never passes note, ticket, goal or owner id")
def _():
    rows = webapp._plan_public_rows([item(), item(id="i2", internal=True)],
                                    {"people": False, "hours": False})
    assert len(rows) == 1
    r = rows[0]
    for k in ("note", "ticket", "ticket_url", "goal", "goal_id", "internal", "project_id"):
        assert k not in r, k
    assert r["owner"] == "" and r["owner_id"] is None
    assert r["estimate"] is None and r["tracked"] is None


@check("the strip shows owners and hours only when the flags say so")
def _():
    src = [dict(item(), tracked=4.0)]
    r = webapp._plan_public_rows(src, {"people": True, "hours": True})[0]
    assert r["owner"] == "Ana" and r["estimate"] == 10.0 and r["tracked"] == 4.0
    r = webapp._plan_public_rows(src, {"people": True, "hours": False})[0]
    assert r["owner"] == "Ana" and r["estimate"] is None


@check("the strip keeps the geometry the timeline needs")
def _():
    rows = webapp._plan_period_rows([item()], RNG, COLS)
    r = webapp._plan_public_rows(rows, {})[0]
    assert r["col_a"] == 2 and r["col_b"] == 4 and r["status"] == "Planned"


# ---- item validation -------------------------------------------------------

@check("a milestone-style single day stores no end; a range stores both as plain dates")
def _():
    assert ops._plan_dates("2026-09-08", "2026-09-08") == {"date": {"start": "2026-09-08", "end": None}}
    assert ops._plan_dates("2026-09-08", "2026-09-10")["date"]["end"] == "2026-09-10"
    assert ops._plan_dates(None, None) == {"date": None}


@check("an end before the start, or a span over a year, is refused")
def _():
    for a, b in (("2026-09-10", "2026-09-08"), ("2026-01-01", "2027-06-01")):
        try:
            ops._plan_dates(a, b)
            assert False, "accepted"
        except ValueError:
            pass


@check("only the keys sent are written, so a status change can't blank the note")
def _():
    props = ops._plan_props({"status": "Done"})
    assert list(props) == ["Status"]
    assert props["Status"]["select"]["name"] == "Done"


@check("unknown status/type, an empty name, a stranger as owner and a non-Notion ticket are refused")
def _():
    for fields in ({"status": "Wat"}, {"type": "Epic"}, {"name": "  "},
                   {"owner_id": "u9"}, {"ticket_url": "https://example.com/x"}):
        try:
            ops._plan_props(fields, people_ids={"u1"})
            assert False, f"accepted {fields}"
        except ValueError:
            pass


@check("empty estimate clears it (unestimated), 0 stays 0")
def _():
    assert ops._plan_props({"estimate": ""})["Estimate"] == {"number": None}
    assert ops._plan_props({"estimate": 0})["Estimate"] == {"number": 0.0}


@check("a pasted Notion link fills the ticket label from its slug")
def _():
    url = "https://app.notion.com/p/Fix-login-bug-0123456789abcdef0123456789abcdef"
    props = ops._plan_props({"ticket_url": url})
    assert props["Ticket URL"]["url"].startswith("https://")
    assert "Fix login bug" in props["Ticket"]["rich_text"][0]["text"]["content"]


# ---- tracked hours ---------------------------------------------------------

@check("tracked: a goal wins, a ticket sums matching entries, neither means absent")
def _():
    tid = "0123456789abcdef0123456789abcdef"
    turl = f"https://www.notion.so/Thing-{tid}"
    old = (ops.goal_totals, ops.project_entries, ops.GOALS_DS)
    ops.GOALS_DS = "goals"
    ops.goal_totals = lambda pid: {"g1": 12.5}
    ops.project_entries = lambda pid, a, b: [
        {"task_url": turl, "hours": 2}, {"task_url": turl, "hours": 1.5},
        {"task_url": "https://www.notion.so/Other-" + "f" * 32, "hours": 9}, {"task_url": "", "hours": 4}]
    try:
        items = [item(id="g", goal_id="g1"), item(id="t", ticket_url=turl),
                 item(id="n"), item(id="g0", goal_id="g-unknown")]
        out = ops.plan_tracked("p1", items, RNG["from"], RNG["to"])
        assert out == {"g": 12.5, "t": 3.5, "g0": 0.0}, out
    finally:
        ops.goal_totals, ops.project_entries, ops.GOALS_DS = old


# ---- the share token -------------------------------------------------------

@check("a malformed or short token never reaches Notion")
def _():
    old = ops._notion
    class Boom:
        class data_sources:
            @staticmethod
            def query(**kw):
                raise AssertionError("queried")
    ops._notion = Boom()
    old_ds = ops.PLAN_DS
    ops.PLAN_DS = "plan"
    try:
        for t in ("", "short", "x" * 100, "has space here and more chars!!", "../../etc/passwd-aaaaaaaaaa"):
            assert ops.project_by_share_token(t) is None
    finally:
        ops._notion, ops.PLAN_DS = old, old_ds


@check("a token resolves to its project and is cached; a revoked one stops within the TTL")
def _():
    tok = "A" * 43
    calls = []
    class N:
        class data_sources:
            @staticmethod
            def query(**kw):
                calls.append(kw["filter"]["rich_text"]["equals"])
                return {"results": [{"id": "p1", "properties": {
                    "Name": {"title": [{"plain_text": "Acme"}]},
                    "Active": {"checkbox": True},
                    ops.SHARE_TOKEN_PROP: {"rich_text": [{"plain_text": tok}]},
                    ops.SHARE_PEOPLE_PROP: {"checkbox": True},
                }}]}
    old = (ops._notion, ops.PLAN_DS)
    ops._notion, ops.PLAN_DS = N(), "plan"
    ops._share_cache.clear()
    try:
        p = ops.project_by_share_token(tok)
        assert p and p["id"] == "p1" and p["share"]["people"] and not p["share"]["hours"]
        ops.project_by_share_token(tok)
        assert calls == [tok]          # second read came from the cache
    finally:
        ops._notion, ops.PLAN_DS = old
        ops._share_cache.clear()


# ---- the PDF ---------------------------------------------------------------

@check("the PDF builds, names the project, and omits owners/hours when told to")
def _():
    import reportlab.rl_config as rl
    rl.pageCompression = 0
    rows = webapp._plan_period_rows([item(), item(id="m", type="Milestone", start="2026-09-11", end="2026-09-11", name="Launchday")], RNG, COLS)
    for r in rows:
        r["tracked"] = 4.0
    ctx = {"project": "Acme Corp", "period_label": RNG["label"], "date_from": RNG["from"],
           "date_to": RNG["to"], "generated": "2026-09-09", "show_people": False,
           "show_hours": False, "pm": "Ana", "am": None}
    pdf = plan_pdf.build(ctx, webapp._plan_public_rows(rows, {}))
    assert pdf.startswith(b"%PDF")
    assert b"Acme Corp" in pdf and b"Launchday" in pdf
    assert b"Ana" not in pdf and b"Tracked" not in pdf
    full = plan_pdf.build(dict(ctx, show_people=True, show_hours=True), rows)
    assert b"Ana" in full and b"Tracked" in full


@check("an empty plan still produces a PDF")
def _():
    ctx = {"project": "Acme", "period_label": "w", "date_from": RNG["from"], "date_to": RNG["to"],
           "generated": "2026-09-09"}
    assert plan_pdf.build(ctx, []).startswith(b"%PDF")


# ---- the pages -------------------------------------------------------------

class FakeOps:
    """Just enough of notion_ops for /plan and /p/<token> to render."""
    def __init__(self):
        self.items = [item(), item(id="i2", name="Secret thing", internal=True),
                      item(id="i3", name="Later", start=None, end=None, status="Backlog")]
        self.share = {"token": "T" * 43, "people": False, "hours": False}
        self.project = {"id": "p1", "name": "Acme", "active": True, "pm_id": "u2", "am_id": None,
                        "budget": None, "partner": "", "umbrella": False}

    def install(self):
        self.old = {k: getattr(ops, k) for k in ("plan_enabled", "list_projects", "list_plan_items",
                                                  "plan_tracked", "list_people", "plan_share",
                                                  "list_goals", "project_by_share_token", "access_ids")}
        ops.plan_enabled = lambda: True
        ops.list_projects = lambda *a, **k: [self.project]
        ops.list_plan_items = lambda pid: [dict(i) for i in self.items]
        ops.plan_tracked = lambda pid, items, a, b: {"i1": 4.0}
        ops.list_people = lambda: [{"id": "u2", "name": "Ana"}, {"id": "u-admin", "name": "Dev"}]
        ops.plan_share = lambda pid: dict(self.share)
        ops.list_goals = lambda *a, **k: []
        ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": {"u-admin"}}
        ops.project_by_share_token = lambda t: (dict(self.project, share=dict(self.share))
                                                if t == self.share["token"] else None)

    def restore(self):
        for k, v in self.old.items():
            setattr(ops, k, v)


def client():
    from fastapi.testclient import TestClient
    return TestClient(webapp.app)


@check("/plan renders the timeline for an admin with the internal item and its note visible")
def _():
    f = FakeOps(); f.install()
    try:
        r = client().get("/plan?project=p1&period=weekly&start=2026-09-09")
        assert r.status_code == 200, r.status_code
        assert "Homepage" in r.text and "Secret thing" in r.text and "Later" in r.text
        assert "secret" in r.text            # the note, in the ITEMS json for the dialog
        assert "/p/" + f.share["token"] in r.text
    finally:
        f.restore()


@check("/plan is admins only")
def _():
    f = FakeOps(); f.install()
    ops.access_ids = lambda: {"allowed": {"u-admin"}, "admins": set()}
    old = os.environ.pop("ADMIN_EMAILS", None)
    try:
        r = client().get("/plan", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
    finally:
        f.restore()
        if old:
            os.environ["ADMIN_EMAILS"] = old


@check("/p/<token> shows the plan without login, minus internal items, notes and owners")
def _():
    f = FakeOps(); f.install()
    try:
        r = client().get(f"/p/{f.share['token']}?start=2026-09-09")
        assert r.status_code == 200
        assert "Homepage" in r.text and "Later" in r.text
        assert "Secret thing" not in r.text and "secret" not in r.text
        assert "Ana" not in r.text
        assert 'href="/"' not in r.text and "/login" not in r.text and "/plan" not in r.text
        assert r.headers["cache-control"] == "no-store"
        assert "noindex" in r.headers["x-robots-tag"]
    finally:
        f.restore()


@check("/p/<token> shows owners once the flag is on")
def _():
    f = FakeOps(); f.share["people"] = True; f.install()
    try:
        r = client().get(f"/p/{f.share['token']}?start=2026-09-09&view=tickets")
        assert "Ana" in r.text
    finally:
        f.restore()


@check("a wrong token is a 404 that names nothing")
def _():
    f = FakeOps(); f.install()
    try:
        r = client().get("/p/" + "Z" * 43)
        assert r.status_code == 404 and "Acme" not in r.text
        r = client().get("/p/" + "Z" * 43 + ".pdf")
        assert r.status_code == 404
    finally:
        f.restore()


@check("/p/<token>.pdf streams a PDF inline")
def _():
    f = FakeOps(); f.install()
    try:
        r = client().get(f"/p/{f.share['token']}.pdf?start=2026-09-09")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert r.headers["content-type"].startswith("application/pdf")
    finally:
        f.restore()


@check("a write with a bad origin is refused; a good one only writes the fields named")
def _():
    f = FakeOps(); f.install()
    seen = {}
    ops.update_plan_item = lambda iid, fields: seen.update(iid=iid, fields=fields) or item()
    try:
        c = client()
        r = c.post("/api/plan/item", json={"item_id": "i1", "status": "Done", "fields": ["status"]},
                   headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        r = c.post("/api/plan/item", json={"item_id": "i1", "status": "Done", "note": "x",
                                           "fields": ["status"]})
        assert r.status_code == 200, r.text
        assert seen["fields"] == {"status": "Done"}
    finally:
        f.restore()
        del ops.update_plan_item


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
