#!/usr/bin/env python3
"""Tests for mirroring approved absences onto the Google Calendar.

Run:  ./.venv/bin/python tests/test_gcal.py

Plain asserts and a tiny runner, like tests/test_budgets.py. **Nothing here
touches Google or Notion**: gcal._request is replaced by a fake that records
calls and answers with whatever status a test asks for, and the endpoints are
driven with ops/auth monkeypatched.
"""
from __future__ import annotations

import os
import string
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-for-anything")

from web import gcal  # noqa: E402
from web import google_auth  # noqa: E402
from web import notion_ops as ops  # noqa: E402
from web import app as webapp  # noqa: E402

_FAILS: list[str] = []
PID = "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809"


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


class Resp:
    def __init__(self, data=None):
        self._data = data or {}

    def json(self):
        return self._data


class FakeGoogle:
    """Stands in for gcal._request. `fail` maps a method to the HTTP status it
    should raise with; `items` is what a GET (event list) returns."""

    def __init__(self, fail=None, items=None):
        self.calls, self.fail, self.items = [], dict(fail or {}), items or []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        status = self.fail.get(method)
        if status:
            raise google_auth.GoogleError(f"HTTP {status}", status)
        return Resp({"items": self.items} if method == "GET" else {})

    def methods(self):
        return [c[0] for c in self.calls]


class env:
    """Turn the feature on (calendar id + Google creds) and install a fake."""

    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self.keep = {k: os.environ.get(k) for k in
                     ("ABSENCES_CALENDAR_ID", *google_auth._VARS)}
        os.environ["ABSENCES_CALENDAR_ID"] = "c_test@group.calendar.google.com"
        for v in google_auth._VARS:
            os.environ[v] = "x"
        self.real = gcal._request
        gcal._request = self.fake
        return self.fake

    def __exit__(self, *exc):
        gcal._request = self.real
        for k, v in self.keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def arow(status="approved", start="2026-10-12", end="2026-10-16", pid=PID, event=""):
    return {"id": pid, "person": "Ana", "person_id": "u1", "start": start, "end": end,
            "status": status, "reason": "Secret reason", "note": "secret note",
            "calendar_event": event}


# ---- pure parts --------------------------------------------------------

@check("event id is deterministic and valid per the Calendar API (base32hex, 5–1024)")
def _():
    eid = gcal.event_id(PID)
    assert eid == gcal.event_id(PID.replace("-", "")) == gcal.event_id(PID.upper())
    assert 5 <= len(eid) <= 1024
    assert set(eid) <= set(string.digits + "abcdefghijklmnopqrstuv"), eid
    assert gcal.event_id(PID) != gcal.event_id(PID[:-1] + "0")
    for bad in ("", "nope", "zz" * 16):
        try:
            gcal.event_id(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should be refused")


@check("all-day end date is the inclusive end + 1 (exclusive), across a month")
def _():
    b = gcal.event_body(arow(start="2026-10-26", end="2026-10-31"))
    assert b["start"] == {"date": "2026-10-26"}
    assert b["end"] == {"date": "2026-11-01"}
    one = gcal.event_body(arow(start="2026-12-31", end="2026-12-31"))
    assert one["end"] == {"date": "2027-01-01"}
    blank = gcal.event_body(dict(arow(start="2026-10-12"), end=""))
    assert blank["end"] == {"date": "2026-10-13"}


@check("event carries name, tag and nothing private")
def _():
    b = gcal.event_body(arow())
    assert b["summary"] == "Ana — Out"
    assert b["extendedProperties"] == {"private": {"absenceId": PID}}
    assert "attendees" not in b and "description" not in b
    assert "Secret" not in str(b) and "secret" not in str(b)


@check("feature is off without ABSENCES_CALENDAR_ID — no calls at all")
def _():
    fake = FakeGoogle()
    with env(fake):
        os.environ.pop("ABSENCES_CALENDAR_ID")
        assert not gcal.enabled()
        gcal.sync_absence(arow())
        gcal.remove_absence(PID)
    assert fake.calls == []


# ---- lifecycle ---------------------------------------------------------

@check("approve inserts the event and records its id")
def _():
    rec = []
    with env(FakeGoogle()) as fake:
        gcal.sync_absence(arow(), record=lambda p, e: rec.append((p, e)))
    assert fake.methods() == ["POST"]
    assert fake.calls[0][2]["json"]["id"] == gcal.event_id(PID)
    assert "c_test%40group.calendar.google.com" in fake.calls[0][1]
    assert rec == [(PID, gcal.event_id(PID))]


@check("re-approve: insert 409 becomes an update of the same id; no re-record")
def _():
    rec = []
    with env(FakeGoogle(fail={"POST": 409})) as fake:
        gcal.sync_absence(arow(event=gcal.event_id(PID)), record=lambda p, e: rec.append(e))
    assert fake.methods() == ["POST", "PUT"]
    assert fake.calls[1][1].endswith("/events/" + gcal.event_id(PID))
    assert fake.calls[1][2]["json"]["status"] == "confirmed"
    assert rec == []


@check("decline deletes the event and clears the stored id")
def _():
    rec = []
    with env(FakeGoogle()) as fake:
        gcal.sync_absence(arow(status="declined", event=gcal.event_id(PID)),
                          record=lambda p, e: rec.append((p, e)))
    assert fake.methods() == ["DELETE"]
    assert fake.calls[0][1].endswith("/events/" + gcal.event_id(PID))
    assert rec == [(PID, "")]


@check("deleting an event that's already gone (404/410) is success")
def _():
    for status in (404, 410):
        rec = []
        with env(FakeGoogle(fail={"DELETE": status})):
            gcal.sync_absence(arow(status="declined", event="x"),
                              record=lambda p, e: rec.append(e))
        assert rec == [""], status


@check("a Google failure is swallowed and nothing is recorded")
def _():
    rec = []
    with env(FakeGoogle(fail={"POST": 403, "DELETE": 500})):
        gcal.sync_absence(arow(), record=lambda p, e: rec.append(e))
        gcal.sync_absence(arow(status="declined", event="x"), record=lambda p, e: rec.append(e))
        gcal.remove_absence(PID)
    assert rec == []


@check("a Notion failure recording the id is swallowed too")
def _():
    def boom(p, e):
        raise RuntimeError("notion down")
    with env(FakeGoogle()):
        gcal.sync_absence(arow(), record=boom)


# ---- endpoints ---------------------------------------------------------

class patched:
    """Swap attributes on modules for the length of a block."""

    def __init__(self, *triples):
        self.triples, self.keep = triples, []

    def __enter__(self):
        for mod, name, val in self.triples:
            self.keep.append((mod, name, getattr(mod, name)))
            setattr(mod, name, val)

    def __exit__(self, *exc):
        for mod, name, val in reversed(self.keep):
            setattr(mod, name, val)


def _client():
    from fastapi.testclient import TestClient
    return TestClient(webapp.app)


USER = {"id": "u2", "name": "Zarco", "email": "z@znlove.xyz"}
H = {"origin": "http://testserver"}


@check("POST /api/absence/decide approved -> event created; declined -> deleted")
def _():
    recorded = []
    for decision, expect in (("approved", ["POST"]), ("declined", ["DELETE"])):
        decided = arow(status=decision)
        with env(FakeGoogle()) as fake, patched(
                (webapp, "_require_login", lambda r: USER),
                (webapp.auth, "is_approver", lambda u: True),
                (webapp.ops, "decide_absence", lambda *a, **k: decided),
                (webapp.ops, "set_absence_calendar_event", lambda p, e: recorded.append(e)),
                (webapp, "_notify_absence_decided", lambda *a: None)):
            res = _client().post("/api/absence/decide", headers=H,
                                 json={"absence_id": PID, "decision": decision})
        assert res.status_code == 200 and res.json()["ok"], res.text
        assert fake.methods() == expect, (decision, fake.methods())


@check("decide still succeeds when Calendar is down")
def _():
    with env(FakeGoogle(fail={"POST": 500})), patched(
            (webapp, "_require_login", lambda r: USER),
            (webapp.auth, "is_approver", lambda u: True),
            (webapp.ops, "decide_absence", lambda *a, **k: arow()),
            (webapp, "_notify_absence_decided", lambda *a: None)):
        res = _client().post("/api/absence/decide", headers=H,
                             json={"absence_id": PID, "decision": "approved"})
    assert res.status_code == 200 and res.json()["ok"], res.text


@check("POST /api/absence/delete removes the event")
def _():
    with env(FakeGoogle()) as fake, patched(
            (webapp, "_require_login", lambda r: USER),
            (webapp.auth, "is_admin", lambda u: True),
            (webapp.ops, "delete_absence", lambda *a, **k: arow())):
        res = _client().post("/api/absence/delete", headers=H, json={"absence_id": PID})
    assert res.status_code == 200 and res.json()["ok"], res.text
    assert fake.methods() == ["DELETE"]


@check("calendar-sync: refused for non-admins and when off; reconciles and reports counts")
def _():
    keep_id, stray = PID, "9" * 32
    rows = [arow(),                                                    # approved -> PUT/POST
            arow(status="declined", pid="2" * 32, event="absence" + "2" * 32),  # -> DELETE
            arow(status="pending", pid="3" * 32)]                      # no event -> nothing
    items = [{"id": gcal.event_id(keep_id), "extendedProperties": {"private": {"absenceId": keep_id}}},
             {"id": gcal.event_id(stray), "extendedProperties": {"private": {"absenceId": stray}}},
             {"id": "someone-elses-event"}]
    recorded = []
    base = ((webapp, "_require_login", lambda r: USER),
            (webapp.ops, "list_absences", lambda *a, **k: rows),
            (webapp.ops, "set_absence_calendar_event", lambda p, e: recorded.append((p, e))))
    with env(FakeGoogle(items=items)) as fake, patched(
            *base, (webapp.auth, "is_admin", lambda u: False),
            (webapp.auth, "is_approver", lambda u: False)):
        res = _client().post("/api/absence/calendar-sync", headers=H)
    assert res.status_code == 403 and fake.calls == []
    with env(FakeGoogle(items=items)) as fake, patched(
            *base, (webapp.auth, "is_admin", lambda u: True)):
        os.environ.pop("ABSENCES_CALENDAR_ID")
        res = _client().post("/api/absence/calendar-sync", headers=H)
    assert res.status_code == 400 and fake.calls == []
    with env(FakeGoogle(items=items)) as fake, patched(
            *base, (webapp.auth, "is_admin", lambda u: True)):
        res = _client().post("/api/absence/calendar-sync", headers=H)
    body = res.json()
    assert res.status_code == 200 and body["ok"], res.text
    assert (body["synced"], body["removed"], body["failed"]) == (1, 2, 0), body
    deleted = [c[1].rsplit("/", 1)[1] for c in fake.calls if c[0] == "DELETE"]
    assert deleted == ["absence" + "2" * 32, gcal.event_id(stray)], deleted
    assert "someone-elses-event" not in str(fake.calls)
    assert (PID, gcal.event_id(PID)) in recorded and ("2" * 32, "") in recorded


@check("ensure_absence_properties adds the Calendar event column")
def _():
    sent = {}

    class DS:
        def retrieve(self, _):
            return {"properties": {p: {} for p in (ops.ABSENCE_STATUS_PROP, ops.ABSENCE_DECIDER_PROP,
                                                   ops.ABSENCE_DECIDED_PROP, ops.ABSENCE_NOTE_PROP)}}

        def update(self, _, properties):
            sent.update(properties)

    class N:
        data_sources = DS()

    with patched((ops, "_notion", N()), (ops, "ABSENCES_DS", "abs-ds")):
        ops.ensure_absence_properties()
    assert sent == {ops.ABSENCE_EVENT_PROP: {"rich_text": {}}}, sent


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
