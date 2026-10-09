"""Approved absences, mirrored onto znlove's shared Google Calendar.

Notion stays the source of truth; the calendar is a read-only picture of it.
An approved absence becomes one all-day event, a decline or a delete takes it
away, and a pending one never appears. Off unless ABSENCES_CALENDAR_ID is set
(and Google is connected — the same refresh token as Gmail and Sheets).

Idempotency comes from the event id, not from remembering anything: it is
derived from the Notion page id, so a retry, a double click or a reconcile can
only ever touch the one event that belongs to that absence. An insert that hits
409 (the id exists, even as a previously deleted event) becomes an update, and
deleting an event that is already gone counts as done.

Every call here is a courtesy, exactly like the absence emails: the decision is
already saved in Notion when these run, so failures are logged and swallowed
by `sync_absence`/`remove_absence` and never reach the person who clicked.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Callable, Optional
from urllib.parse import quote

from . import google_auth

API = "https://www.googleapis.com/calendar/v3"
TAG = "absenceId"     # extendedProperties.private key marking events we own

# The reconcile window: far enough back to fix last month's drift, far enough
# ahead to cover anything booked for next year.
SYNC_BACK_DAYS = 60
SYNC_AHEAD_DAYS = 365


def calendar_id() -> str:
    return os.environ.get("ABSENCES_CALENDAR_ID", "").strip()


def enabled() -> bool:
    return bool(calendar_id()) and google_auth.configured()


def event_id(page_id: str) -> str:
    """A Calendar event id derived from the Notion page id.

    The Calendar API allows 5–1024 characters from base32hex's lowercase
    alphabet (a–v, 0–9). A Notion id is 32 hex digits, and hex is a subset of
    that alphabet, so the dashless id under a fixed prefix (itself only a–v
    letters) is already valid — deterministic, and one event per absence.
    """
    bare = (page_id or "").replace("-", "").lower()
    if len(bare) != 32 or any(c not in "0123456789abcdef" for c in bare):
        raise ValueError(f"not a Notion page id: {page_id!r}")
    return "absence" + bare


def event_body(row: dict) -> dict:
    """The event for one absence row: all-day, inclusive end + 1 day (Calendar's
    all-day `end.date` is exclusive). No reason, no note, no attendees — this
    calendar is shared, the reason isn't."""
    start = dt.date.fromisoformat(row["start"])
    end = dt.date.fromisoformat(row.get("end") or row["start"])
    return {
        "id": event_id(row["id"]),
        "summary": f"{row.get('person') or 'Someone'} — Out",
        "start": {"date": start.isoformat()},
        "end": {"date": (end + dt.timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        # an event id that was deleted earlier lingers as `cancelled`; an
        # update has to say confirmed or the re-approval stays invisible
        "status": "confirmed",
        "extendedProperties": {"private": {TAG: row["id"]}},
    }


def _events_url(eid: str = "") -> str:
    url = f"{API}/calendars/{quote(calendar_id(), safe='')}/events"
    return f"{url}/{eid}" if eid else url


def _request(method: str, url: str, **kw):
    """The one seam to Google — tests replace it."""
    return google_auth.call(method, url, **kw)


def upsert(row: dict) -> str:
    """Create the absence's event, or update it if it already exists. Returns
    the event id."""
    body = event_body(row)
    try:
        _request("POST", _events_url(), json=body)
    except google_auth.GoogleError as e:
        if e.status != 409:
            raise
        _request("PUT", _events_url(body["id"]), json=body)
    return body["id"]


def delete(page_id: str) -> None:
    """Remove the absence's event; one that's already gone is success."""
    try:
        _request("DELETE", _events_url(event_id(page_id)))
    except google_auth.GoogleError as e:
        if e.status not in (404, 410):
            raise


def sync_absence(row: dict, record: Optional[Callable[[str, str], None]] = None) -> None:
    """Make the calendar match one absence row — approved shows, anything else
    doesn't. `record(page_id, event_id)` stores the event id on the Notion row
    ("" clears it). Never raises."""
    if not enabled():
        return
    try:
        if row.get("status") == "approved" and row.get("start"):
            eid = upsert(row)
        else:
            delete(row["id"])
            eid = ""
        if record and (row.get("calendar_event") or "") != eid:
            record(row["id"], eid)
    except Exception as exc:  # noqa: BLE001 — a courtesy must not fail a decision
        logging.warning("absence %s not synced to the calendar: %s", row.get("id"), exc)


def remove_absence(page_id: str) -> None:
    """The event of an absence that was just deleted. Never raises."""
    if not enabled():
        return
    try:
        delete(page_id)
    except Exception as exc:  # noqa: BLE001
        logging.warning("absence %s not removed from the calendar: %s", page_id, exc)


def _tagged_events(time_min: str, time_max: str) -> list[dict]:
    """Every event on the calendar in the window that this app put there."""
    out, token = [], None
    while True:
        params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                  "maxResults": "2500"}
        if token:
            params["pageToken"] = token
        data = _request("GET", _events_url(), params=params).json()
        for ev in data.get("items", []):
            tag = ((ev.get("extendedProperties") or {}).get("private") or {}).get(TAG)
            if tag:
                out.append({"id": ev["id"], "absence": tag})
        token = data.get("nextPageToken")
        if not token:
            return out


def reconcile(rows: list[dict], date_from: str, date_to: str,
              record: Optional[Callable[[str, str], None]] = None) -> dict:
    """Bring the calendar in line with every absence in [date_from, date_to].

    Approved rows are upserted; pending/declined rows lose their event; and an
    event we tagged whose absence is no longer approved (or no longer exists —
    deleted in Notion directly) is removed. Events nobody tagged are never
    touched. Per-row failures are counted, not raised.
    """
    counts = {"synced": 0, "removed": 0, "failed": 0}
    approved = {r["id"] for r in rows if r.get("status") == "approved" and r.get("start")}
    for r in rows:
        try:
            if r["id"] in approved:
                eid = upsert(r)
                counts["synced"] += 1
            else:
                eid = ""
                if r.get("calendar_event"):
                    delete(r["id"])
                    counts["removed"] += 1
            if record and (r.get("calendar_event") or "") != eid:
                record(r["id"], eid)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            logging.warning("absence %s not reconciled: %s", r.get("id"), exc)
    # strays: tagged events whose absence isn't approved any more
    keep = {event_id(i) for i in approved}
    end = (dt.date.fromisoformat(date_to) + dt.timedelta(days=1)).isoformat()
    for ev in _tagged_events(f"{date_from}T00:00:00Z", f"{end}T00:00:00Z"):
        if ev["id"] in keep:
            continue
        try:
            _request("DELETE", _events_url(ev["id"]))
            counts["removed"] += 1
        except google_auth.GoogleError as e:
            if e.status not in (404, 410):
                counts["failed"] += 1
                logging.warning("calendar event %s not removed: %s", ev["id"], e)
    return counts
