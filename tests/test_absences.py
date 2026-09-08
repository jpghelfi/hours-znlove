#!/usr/bin/env python3
"""Tests for the absence approval flow.

Run:  ./.venv/bin/python tests/test_absences.py

Plain asserts and a tiny runner rather than pytest — the project has no test
dependency and this shouldn't be the change that adds one (see
tests/test_budgets.py, which this mirrors).

**Nothing here touches Notion.** The module-level client is built at import (so
a NOTION_TOKEN must exist in .env), but every function that would make a call is
monkeypatched. What's worth testing is the part a page load won't show you: a
legacy row reading as approved, the board counting approved days only while
still drawing the pending ones, the two guards on decide_absence, and who
counts as an approver.
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

from web import auth  # noqa: E402
from web import mailer  # noqa: E402
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


def page(start="2026-09-07", end="2026-09-11", status=None, person="u1",
         reason="Holiday", decided_by=None, decided_at=None, note="",
         parent=None, pid="abs1"):
    """One Absences page as Notion returns it. `status=None` = a legacy row."""
    props = {
        "Dates": {"date": {"start": start, "end": end}},
        "Person": {"people": [{"id": person}] if person else []},
        "Days": {"number": 5},
        "Reason": {"rich_text": [{"plain_text": reason}]},
    }
    if status is not None:
        props[ops.ABSENCE_STATUS_PROP] = {"select": {"name": status}}
    if decided_by:
        props[ops.ABSENCE_DECIDER_PROP] = {"people": [{"id": decided_by}]}
    if decided_at:
        props[ops.ABSENCE_DECIDED_PROP] = {"date": {"start": decided_at}}
    if note:
        props[ops.ABSENCE_NOTE_PROP] = {"rich_text": [{"plain_text": note}]}
    return {"id": pid, "url": "", "properties": props,
            "parent": {"type": "data_source_id",
                       "data_source_id": parent or (ops.ABSENCES_DS or "abs-ds")}}


PEOPLE = {"u1": "Ana", "u2": "Zarco"}


def row(**kw):
    return ops._absence_row(page(**kw), PEOPLE)


# ---- parsing -----------------------------------------------------------

@check("a legacy row with no Status reads as approved")
def _():
    # Rows filed before approvals existed were logged under the old "you log
    # it, nobody signs it" rule. Reading them as pending would drop days off
    # the board and put a queue in front of people who never joined one.
    r = row(status=None)
    assert r["status"] == "approved", r["status"]
    assert r["decided_by"] is None and r["decided_at"] == "" and r["note"] == ""


@check("each of the three statuses parses")
def _():
    assert row(status="Pending")["status"] == "pending"
    assert row(status="Approved")["status"] == "approved"
    assert row(status="Declined")["status"] == "declined"


@check("an empty select reads as approved, not as a crash")
def _():
    p = page()
    p["properties"][ops.ABSENCE_STATUS_PROP] = {"select": None}
    assert ops._absence_row(p, PEOPLE)["status"] == "approved"


@check("a renamed Status column reads as approved, never as a 500")
def _():
    # The alloc_person_prop lesson: this app has been taken down by a Notion
    # column rename twice. Absences must fall back to the old rule, not error.
    p = page(status="Pending")
    p["properties"]["Statuz"] = p["properties"].pop(ops.ABSENCE_STATUS_PROP)
    assert ops._absence_row(p, PEOPLE)["status"] == "approved"


@check("an unknown status name degrades to approved")
def _():
    assert row(status="Maybe")["status"] == "approved"


@check("the decider is resolved against the roster, not the payload")
def _():
    # People properties come back nameless ({"object": "user", "id": …}).
    r = row(status="Approved", decided_by="u2", decided_at="2026-09-08", note="ok")
    assert r["decided_by"] == "u2" and r["decided_by_name"] == "Zarco"
    assert r["decided_at"] == "2026-09-08" and r["note"] == "ok"


# ---- the board ---------------------------------------------------------

WEEK = {"from": "2026-09-07", "to": "2026-09-13"}   # Mon 7th – Sun 13th


def arow(pid, start, end, status="approved", person="u1", reason="Holiday"):
    return {"id": pid, "person_id": person, "person": PEOPLE.get(person, "?"),
            "start": start, "end": end, "days": 0, "reason": reason,
            "status": status, "decided_by": None, "decided_by_name": "",
            "decided_at": "", "note": ""}


@check("_absence_days counts one status at a time")
def _():
    rows = [arow("a", "2026-09-07", "2026-09-08"),
            arow("b", "2026-09-09", "2026-09-09", status="pending"),
            arow("c", "2026-09-10", "2026-09-10", status="declined")]
    approved = webapp._absence_days(rows, WEEK)
    pending = webapp._absence_days(rows, WEEK, "pending")
    assert len(approved["u1"]) == 2, approved
    assert list(pending["u1"]) == [dt.date(2026, 9, 9)], pending
    assert dt.date(2026, 9, 10) not in approved["u1"]


@check("a row with no status at all still counts as approved")
def _():
    # _absence_days defaults, so a caller that hands it a hand-built row (or a
    # row from before the flow) doesn't silently lose the day.
    r = arow("a", "2026-09-07", "2026-09-07")
    r.pop("status")
    assert webapp._absence_days([r], WEEK)["u1"]


@check("the board totals count approved days only")
def _():
    cols = webapp._absence_columns("weekly", WEEK)
    rows = [arow("a", "2026-09-07", "2026-09-08"),
            arow("b", "2026-09-09", "2026-09-10", status="pending")]
    board, totals = webapp._absence_board(rows, cols, WEEK, [{"id": "u1", "name": "Ana"}])
    assert totals == [1, 1, 0, 0, 0], totals
    assert board[0]["days"] == 2 and board[0]["pending"] == 2


@check("a pending day is drawn hollow, and carries its reason")
def _():
    cols = webapp._absence_columns("weekly", WEEK)
    rows = [arow("b", "2026-09-09", "2026-09-09", status="pending", reason="Wedding")]
    board, totals = webapp._absence_board(rows, cols, WEEK, [{"id": "u1", "name": "Ana"}])
    wed = board[0]["cells"][2]
    assert wed["n"] == 0 and wed["p"] == 1
    assert wed["label"] == "" and wed["plabel"] == "○"
    assert wed["why"] == "pending: Wedding", wed["why"]
    assert sum(totals) == 0        # asked for is not off


@check("the monthly view marks pending as n?")
def _():
    month = {"from": "2026-09-01", "to": "2026-09-30"}
    cols = webapp._absence_columns("monthly", month)
    rows = [arow("b", "2026-09-08", "2026-09-09", status="pending")]
    board, _t = webapp._absence_board(rows, cols, month, [{"id": "u1", "name": "Ana"}])
    week = next(c for c in board[0]["cells"] if c["p"])
    assert week["plabel"] == "2?" and week["label"] == ""


@check("someone whose only absence is pending still gets a board row")
def _():
    cols = webapp._absence_columns("weekly", WEEK)
    rows = [arow("b", "2026-09-09", "2026-09-09", status="pending")]
    board, _t = webapp._absence_board(rows, cols, WEEK, [{"id": "u1", "name": "Ana"}])
    assert len(board) == 1 and board[0]["days"] == 0


# ---- deciding ----------------------------------------------------------

class fake_pages:
    """Swap ops._notion for one that serves a page and records the update."""

    def __init__(self, pg):
        self.pg, self.updated = pg, None

    def __enter__(self):
        outer = self

        class _N:
            class pages:
                @staticmethod
                def retrieve(pid):
                    return outer.pg

                @staticmethod
                def update(pid, **kw):
                    outer.updated = (pid, kw)
                    return outer.pg

        self._n, self._m = ops._notion, ops._person_name_map
        ops._notion = _N
        ops._person_name_map = lambda: PEOPLE
        return self

    def __exit__(self, *exc):
        ops._notion, ops._person_name_map = self._n, self._m


@check("decide_absence refuses a decision that isn't one")
def _():
    with fake_pages(page(status="Pending")) as f:
        for bad in ("maybe", "", "Approved", "pending"):
            try:
                ops.decide_absence("abs1", bad, "u2")
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} should not be a decision")
        assert f.updated is None       # nothing was written


@check("decide_absence refuses a page from another data source")
def _():
    # The id comes from the browser, so a page that isn't an absence must be
    # refused *before* the write — the delete_absence guard.
    with fake_pages(page(parent="some-other-ds")) as f:
        try:
            ops.decide_absence("abs1", "approved", "u2")
        except ValueError as e:
            assert "not an absence" in str(e)
        else:
            raise AssertionError("should have refused a foreign page")
        assert f.updated is None


@check("approving writes Status, decider, date and note")
def _():
    with fake_pages(page(status="Pending")) as f:
        r = ops.decide_absence("abs1", "approved", "u2", "have fun")
    pid, kw = f.updated
    props = kw["properties"]
    assert pid == "abs1"
    assert props[ops.ABSENCE_STATUS_PROP]["select"]["name"] == ops.STATUS_APPROVED
    assert props[ops.ABSENCE_DECIDER_PROP]["people"] == [{"id": "u2"}]
    assert props[ops.ABSENCE_DECIDED_PROP]["date"]["start"] == dt.date.today().isoformat()
    assert props[ops.ABSENCE_NOTE_PROP]["rich_text"][0]["text"]["content"] == "have fun"
    # the returned row reports what was just written, not the pre-write page
    assert r["status"] == "approved" and r["decided_by_name"] == "Zarco"
    assert r["note"] == "have fun"


@check("a decision can be changed back")
def _():
    with fake_pages(page(status="Approved", decided_by="u2")) as f:
        r = ops.decide_absence("abs1", "declined", "u2", "clash")
    assert f.updated[1]["properties"][ops.ABSENCE_STATUS_PROP]["select"]["name"] == \
        ops.STATUS_DECLINED
    assert r["status"] == "declined"


@check("a decision note is capped, not rejected")
def _():
    with fake_pages(page(status="Pending")) as f:
        ops.decide_absence("abs1", "declined", "u2", "x" * 900)
    written = f.updated[1]["properties"][ops.ABSENCE_NOTE_PROP]["rich_text"][0]
    assert len(written["text"]["content"]) == ops.MAX_ABSENCE_NOTE


# ---- who approves ------------------------------------------------------

class fake_access:
    def __init__(self, **sets):
        self.sets = {"allowed": set(), "admins": set(), "approvers": set()}
        self.sets.update(sets)

    def __enter__(self):
        self._a = ops.access_ids
        ops.access_ids = lambda: self.sets
        return self

    def __exit__(self, *exc):
        ops.access_ids = self._a


@check("an approver is matched by People-db id")
def _():
    with fake_access(approvers={"u2"}):
        assert auth.is_approver({"id": "u2", "email": "nobody@example.com"}) is True
        assert auth.is_approver({"id": "u1", "email": "nobody@example.com"}) is False
        assert auth.is_approver(None) is False


@check("ABSENCE_APPROVER_EMAILS is the fallback, and defaults to the two of them")
def _():
    old = os.environ.pop("ABSENCE_APPROVER_EMAILS", None)
    try:
        with fake_access():                       # People db says nobody
            assert auth.is_approver({"id": "u9", "email": "zarco@znlove.xyz"}) is True
            assert auth.is_approver({"id": "u9", "email": "JP.Ghelfi@ZNLove.xyz"}) is True
            assert auth.is_approver({"id": "u9", "email": "someone@znlove.xyz"}) is False
            os.environ["ABSENCE_APPROVER_EMAILS"] = "someone@znlove.xyz"
            assert auth.is_approver({"id": "u9", "email": "someone@znlove.xyz"}) is True
            assert auth.is_approver({"id": "u9", "email": "zarco@znlove.xyz"}) is False
    finally:
        os.environ.pop("ABSENCE_APPROVER_EMAILS", None)
        if old is not None:
            os.environ["ABSENCE_APPROVER_EMAILS"] = old


@check("approver and admin are different questions")
def _():
    # Six admins, two approvers: neither implies the other.
    with fake_access(admins={"u1"}, approvers={"u2"}):
        assert auth.is_admin({"id": "u1"}) and not auth.is_approver({"id": "u1"})
        assert auth.is_approver({"id": "u2"}) and not auth.is_admin({"id": "u2"})


@check("access_ids returns three sets even with no People db")
def _():
    old = ops.PEOPLE_DS
    ops.PEOPLE_DS = None
    try:
        ids = ops.access_ids()
        assert set(ids) == {"allowed", "admins", "approvers"}
        assert all(v == set() for v in ids.values())
    finally:
        ops.PEOPLE_DS = old


# ---- the switch --------------------------------------------------------

@check("absence emails are off unless their own switch is on")
def _():
    old = os.environ.pop("ABSENCE_EMAIL_ENABLED", None)
    try:
        assert mailer.absence_email_enabled() is False
        assert mailer.absence_transport() == ""
        os.environ["ABSENCE_EMAIL_ENABLED"] = "1"
        assert mailer.absence_email_enabled() is True
    finally:
        os.environ.pop("ABSENCE_EMAIL_ENABLED", None)
        if old is not None:
            os.environ["ABSENCE_EMAIL_ENABLED"] = old


@check("send_plain names the channel's own switch when it refuses")
def _():
    # "set BUDGET_ALERTS_ENABLED=1" is useless advice to someone waiting on an
    # absence email — the message has to name the variable that's actually off.
    keep = {k: os.environ.pop(k, None)
            for k in ("BUDGET_ALERTS_ENABLED", "ABSENCE_EMAIL_ENABLED")}
    try:
        for channel, var in (("budget", "BUDGET_ALERTS_ENABLED"),
                             ("absence", "ABSENCE_EMAIL_ENABLED"),
                             (None, "BUDGET_ALERTS_ENABLED")):   # default channel
            kw = {} if channel is None else {"channel": channel}
            try:
                mailer.send_plain(["a@b.co"], "s", "b", **kw)
            except mailer.NotConfigured as e:
                assert var + "=1" in str(e), (channel, str(e))
            else:
                raise AssertionError("a switched-off channel must refuse")
    finally:
        for k, v in keep.items():
            if v is not None:
                os.environ[k] = v


@check("absence recipients default to the two approvers")
def _():
    old = os.environ.pop("ABSENCE_APPROVER_TO", None)
    try:
        assert mailer.absence_approvers() == ["zarco@znlove.xyz", "jp.ghelfi@znlove.xyz"]
        os.environ["ABSENCE_APPROVER_TO"] = "one@znlove.xyz, two@znlove.xyz"
        assert mailer.absence_approvers() == ["one@znlove.xyz", "two@znlove.xyz"]
    finally:
        os.environ.pop("ABSENCE_APPROVER_TO", None)
        if old is not None:
            os.environ["ABSENCE_APPROVER_TO"] = old


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
