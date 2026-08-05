"""Notion data operations for the web app.

Notion remains the source of truth. This module reads people/projects and
reads/writes Time Entries via the 2025-09-03 data-source API.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# reuse the existing client/config from src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import get_client, load_db_ids  # noqa: E402

_notion = get_client()
_ids = load_db_ids()
TIME_DS = _ids["time_entries_ds_id"]
PROJECTS_DS = _ids["projects_ds_id"]
PEOPLE_DS = _ids.get("people_ds_id")  # optional: roster + access list


def ensure_person_property() -> None:
    """Make sure Time Entries has a Person (people) property; add it if missing.

    The web form self-selects a person, so we store it explicitly (Notion's
    'Logged by' only captures the submitter inside Notion's own UI/forms).
    """
    ds = _notion.data_sources.retrieve(TIME_DS)
    if "Person" not in ds["properties"]:
        _notion.data_sources.update(TIME_DS, properties={"Person": {"people": {}}})


def ensure_task_properties() -> None:
    """Make sure Time Entries can hold the Notion ticket an entry was for.

    Two plain properties, deliberately not a relation: znlove's tickets are
    spread across many boards, and a Notion relation targets exactly one data
    source. `Task URL` is the link, `Task` the label shown in reports/exports.
    """
    ds = _notion.data_sources.retrieve(TIME_DS)
    missing = {}
    if "Task" not in ds["properties"]:
        missing["Task"] = {"rich_text": {}}
    if "Task URL" not in ds["properties"]:
        missing["Task URL"] = {"url": {}}
    if missing:
        _notion.data_sources.update(TIME_DS, properties=missing)


def ensure_admin_property() -> None:
    """Make sure the People db has an Admin (checkbox) property; add if missing.

    Access is curated in the People db: an Active row grants login, an Admin
    tick grants the team-wide reports scope (see access_ids). Older People dbs
    predate the Admin column, so add it on startup for existing deployments.
    """
    if not PEOPLE_DS:
        return
    ds = _notion.data_sources.retrieve(PEOPLE_DS)
    if "Admin" not in ds["properties"]:
        _notion.data_sources.update(PEOPLE_DS, properties={"Admin": {"checkbox": {}}})


# ---- reads -------------------------------------------------------------


def list_people() -> list[dict]:
    """The roster shown everywhere (assignments columns, schedule rows,
    person dropdowns).

    Source of truth is the People database (created/seeded by
    src/setup_people_db.py): one row per person, curated in Notion — untick
    Active to hide someone, retitle to rename. Falls back to the raw workspace
    member list when the People db isn't configured — or when querying it
    fails (bad PEOPLE_DS_ID), so a misconfig degrades to the old roster
    instead of a 500 on every page.
    """
    people = None
    if PEOPLE_DS:
        try:
            people = _people_from_db()
        except Exception:
            logging.exception(
                "People db query failed — check PEOPLE_DS_ID (must be the data source id, "
                "people_ds_id in databases.json). Falling back to workspace members."
            )
    if people is None:
        people = _people_from_workspace()
    people.sort(key=lambda p: p["name"].lower())
    return people


def _people_from_db() -> list[dict]:
    people = {}  # user id -> entry; keyed so duplicate rows for one user can't duplicate columns
    kwargs = {
        "data_source_id": PEOPLE_DS, "page_size": 100,
        "filter": {"property": "Active", "checkbox": {"equals": True}},
    }
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            linked = props.get("Person", {}).get("people", [])
            if not linked:  # no Notion user linked -> can't be assigned or log hours
                continue
            title = props.get("Name", {}).get("title", [])
            name = title[0]["plain_text"].strip() if title else ""
            uid = linked[0]["id"]
            people.setdefault(uid, {"id": uid, "name": name or linked[0].get("name") or "(unnamed)"})
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return list(people.values())


def _people_from_workspace() -> list[dict]:
    """Workspace members (real people, not bots)."""
    people = []
    start = None
    while True:
        res = _notion.users.list(start_cursor=start, page_size=100) if start else _notion.users.list(page_size=100)
        for u in res["results"]:
            if u.get("type") == "person":
                people.append({"id": u["id"], "name": u.get("name") or "(unnamed)"})
        if not res.get("has_more"):
            break
        start = res["next_cursor"]
    return people


# ---- access control (login allowlist + admins) --------------------------
#
# Who may log in and who is an admin is curated in the People db, matched by
# the linked Notion user id (the same id OAuth hands back at login): every
# Active row grants login, an additionally-ticked Admin row grants the
# team-wide reports scope. auth.py layers the env-var lists on top as a
# fallback, so a misconfigured People db can't lock everyone out.
#
# is_admin() is checked several times per request, so the derived id sets are
# cached briefly rather than re-queried each call; Notion edits take effect
# within _ACCESS_TTL seconds.
_ACCESS_TTL = 60.0
_access_cache: dict = {"at": 0.0, "allowed": None, "admins": None}
_access_lock = threading.Lock()


def _access_from_db() -> tuple[set, set]:
    """Return (allowed_ids, admin_ids) from the People db.

    allowed = every Active row's linked Notion user; admins = those also ticked
    Admin (an inactive row grants nothing). Rows with no linked Person can't map
    to a login, so they're skipped.
    """
    allowed: set = set()
    admins: set = set()
    kwargs = {
        "data_source_id": PEOPLE_DS, "page_size": 100,
        "filter": {"property": "Active", "checkbox": {"equals": True}},
    }
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            linked = props.get("Person", {}).get("people", [])
            if not linked:
                continue
            uid = linked[0]["id"]
            allowed.add(uid)
            if props.get("Admin", {}).get("checkbox", False):
                admins.add(uid)
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return allowed, admins


def access_ids() -> dict:
    """Cached {"allowed": set, "admins": set} of Notion user ids from the People
    db. Returns empty sets (so callers fall back to the env allowlists) when the
    People db isn't configured or the query fails, rather than 500ing a login."""
    if not PEOPLE_DS:
        return {"allowed": set(), "admins": set()}
    now = time.monotonic()
    with _access_lock:
        if _access_cache["allowed"] is not None and now - _access_cache["at"] < _ACCESS_TTL:
            return {"allowed": _access_cache["allowed"], "admins": _access_cache["admins"]}
    try:
        allowed, admins = _access_from_db()
    except Exception:
        logging.exception(
            "People access query failed — check PEOPLE_DS_ID. Falling back to the "
            "env allowlists (ALLOWED_EMAILS / ADMIN_EMAILS) for this check."
        )
        # Cache the empty result too: a persistent misconfig would otherwise
        # re-query Notion on every is_admin call. Env admins still get through.
        allowed, admins = set(), set()
    with _access_lock:
        _access_cache.update(at=now, allowed=allowed, admins=admins)
    return {"allowed": allowed, "admins": admins}


def get_user(user_id: str) -> dict:
    """Resolve a Notion user id to {id, name, email} using the integration token."""
    u = _notion.users.retrieve(user_id)
    return {
        "id": u["id"],
        "name": u.get("name") or "(unnamed)",
        "email": (u.get("person") or {}).get("email"),
        "avatar": u.get("avatar_url"),
    }


def list_projects(active_only: bool = True, member_of: str | None = None,
                  include_members: bool = False) -> list[dict]:
    """List projects. If member_of (a Notion user id) is given, return only
    projects that user is a member of (the People property includes them).
    If include_members, each project dict also carries "member_ids" (every
    id in the People property), for the schedule page's assignment view."""
    projects = []
    kwargs = {"data_source_id": PROJECTS_DS, "page_size": 100}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            title = props["Name"]["title"]
            name = title[0]["plain_text"] if title else "(untitled)"
            active = props.get("Active", {}).get("checkbox", True)
            if active_only and not active:
                continue
            members = [p["id"] for p in props.get("People", {}).get("people", [])]
            if member_of is not None and member_of not in members:
                continue
            project = {"id": row["id"], "name": name}
            if include_members:
                project["member_ids"] = members
            projects.append(project)
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    projects.sort(key=lambda p: p["name"].lower())
    return projects


def _project_name_map() -> dict:
    return {p["id"]: p["name"] for p in list_projects(active_only=False)}


def _person_name_map() -> dict:
    """Notion user id -> display name, from the roster.

    People *properties* come back as bare user refs ({"object": "user", "id":
    …}) with no name, so anything reading a people property has to resolve
    names itself rather than trusting the payload.
    """
    return {p["id"]: p["name"] for p in list_people()}


def _row_person(props) -> tuple[str | None, str]:
    """Return (person_id, person_name), preferring Person, falling back to Logged by."""
    people = props.get("Person", {}).get("people", [])
    if people:
        return people[0]["id"], people[0].get("name") or "(unnamed)"
    lb = props.get("Logged by", {}).get("created_by", {})
    if lb.get("type") == "person":
        return lb["id"], lb.get("name") or "(unnamed)"
    return None, "(unassigned)"


def entries_between(date_from: str, date_to: str, person_id: str | None = None) -> list[dict]:
    """All entries in [date_from, date_to] (ISO dates), optionally for one person."""
    pname = _project_name_map()
    out = []
    kwargs = {"data_source_id": TIME_DS, "page_size": 100, "filter": {"and": [
        {"property": "Date", "date": {"on_or_after": date_from}},
        {"property": "Date", "date": {"on_or_before": date_to}},
    ]}}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            pid, person = _row_person(props)
            if person_id and pid != person_id:
                continue
            date = props["Date"]["date"]
            rel = props["Project"]["relation"]
            desc = props["Description"]["rich_text"]
            out.append({
                # the entry's own page id, so a reader can offer to edit that
                # exact row (see set_entry_hours)
                "id": row["id"],
                "person_id": pid, "person": person,
                # project_id as well as the name: /project's All view groups by
                # id, so two projects sharing a name can't be merged into one
                "project_id": rel[0]["id"] if rel else None,
                "project": pname.get(rel[0]["id"], "(none)") if rel else "(none)",
                "date": date["start"][:10] if date else None,
                "hours": props["Hours"]["number"] or 0,
                "description": desc[0]["plain_text"] if desc else "",
                **entry_task(props),
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return [e for e in out if e["date"]]


def project_entries(project_id: str, date_from: str, date_to: str) -> list[dict]:
    """Every entry logged against one project in [date_from, date_to].

    Filters on the Project relation in the Notion query (not a Python scan) so
    a busy Time Entries db doesn't get paged through in full for one project.
    """
    out = []
    kwargs = {"data_source_id": TIME_DS, "page_size": 100, "filter": {"and": [
        {"property": "Date", "date": {"on_or_after": date_from}},
        {"property": "Date", "date": {"on_or_before": date_to}},
        {"property": "Project", "relation": {"contains": project_id}},
    ]}}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            date = props["Date"]["date"]
            if not date:
                continue
            pid, person = _row_person(props)
            desc = props["Description"]["rich_text"]
            out.append({
                "id": row["id"],
                "person_id": pid, "person": person,
                "date": date["start"][:10],
                "hours": props["Hours"]["number"] or 0,
                "description": desc[0]["plain_text"] if desc else "",
                **entry_task(props),
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return out


# ---- writes ------------------------------------------------------------

def create_entry(person_id: str | None, project_id: str, date: str, hours: float,
                 description: str = "", task_url: str = "", task_label: str = "") -> None:
    pname_map = _project_name_map()
    props = {
        "Entry": {"title": [{"text": {"content": f"{pname_map.get(project_id, 'Entry')} — {date}"}}]},
        "Project": {"relation": [{"id": project_id}]},
        "Date": {"date": {"start": date}},
        "Hours": {"number": hours},
        "Description": {"rich_text": [{"text": {"content": description}}]},
    }
    if person_id:
        props["Person"] = {"people": [{"id": person_id}]}
    if task_url:
        # Written only when there is a link: an entry with no ticket leaves both
        # properties untouched, so nothing changes for the CLI or Notion forms.
        props[_TASK_URL_PROP] = {"url": task_url}
        props[_TASK_PROP] = {"rich_text": [{"text": {"content": task_label[:200]}}]}
    _notion.pages.create(parent={"type": "data_source_id", "data_source_id": TIME_DS}, properties=props)


# ---- weekly grid -------------------------------------------------------

def monday_of(d: dt.date | None = None) -> dt.date:
    d = d or dt.date.today()
    return d - dt.timedelta(days=d.weekday())  # Monday=0


def week_days(monday: dt.date) -> list[dt.date]:
    return [monday + dt.timedelta(days=i) for i in range(5)]  # Mon..Fri


def week_grid(monday: dt.date, person_id: str) -> dict:
    """Build the Mon–Fri grid for a single person: rows keyed by project."""
    days = week_days(monday)
    day_isos = [d.isoformat() for d in days]
    pname_map = _project_name_map()

    entries = []
    kwargs = {
        "data_source_id": TIME_DS,
        "page_size": 100,
        "filter": {"and": [
            {"property": "Date", "date": {"on_or_after": day_isos[0]}},
            {"property": "Date", "date": {"on_or_before": day_isos[-1]}},
        ]},
    }
    while True:
        res = _notion.data_sources.query(**kwargs)
        entries.extend(res["results"])
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]

    rows: dict = {}
    for e in entries:
        props = e["properties"]
        rel = props["Project"]["relation"]
        if not rel:
            continue
        pid, _ = _row_person(props)
        if pid != person_id:  # each person sees only their own hours
            continue
        project_id = rel[0]["id"]
        date = props["Date"]["date"]["start"][:10] if props["Date"]["date"] else None
        hours = props["Hours"]["number"] or 0
        if date not in day_isos:
            continue
        row = rows.setdefault(project_id, {
            "project_id": project_id,
            "project_name": pname_map.get(project_id, "(none)"),
            "cells": {iso: 0.0 for iso in day_isos},
        })
        row["cells"][date] += hours

    ordered = sorted(rows.values(), key=lambda r: r["project_name"].lower())
    for r in ordered:
        r["total"] = round(sum(r["cells"].values()), 2)
    day_totals = {iso: round(sum(r["cells"][iso] for r in ordered), 2) for iso in day_isos}
    return {
        "monday": monday,
        "days": days,
        "day_isos": day_isos,
        "rows": ordered,
        "day_totals": day_totals,
        "grand_total": round(sum(day_totals.values()), 2),
    }


# ---- forecast / allocations ---------------------------------------------

ALLOC_DS = _ids.get("allocations_ds_id")

_alloc_person_cache: dict = {"at": 0.0, "name": None}
_alloc_person_lock = threading.Lock()
_ALLOC_PROP_TTL = 300.0  # seconds


def alloc_person_prop() -> str:
    """Name of the Allocations people property — "Person" unless it's been
    renamed in Notion.

    The API addresses properties by name, so renaming that column in the Notion
    UI used to 500 every page that reads or writes an allocation (the schedule,
    the reports forecast). Resolve the name from the data source's schema
    instead — preferring "Person", else the one people-typed property — and
    cache it briefly, since this is consulted on every allocation read/write.
    """
    now = time.monotonic()
    with _alloc_person_lock:
        if _alloc_person_cache["name"] and now - _alloc_person_cache["at"] < _ALLOC_PROP_TTL:
            return _alloc_person_cache["name"]
    name = "Person"
    try:
        props = _notion.data_sources.retrieve(data_source_id=ALLOC_DS)["properties"]
        if "Person" not in props:
            people_props = [k for k, v in props.items() if v.get("type") == "people"]
            if len(people_props) == 1:
                name = people_props[0]
                logging.warning(
                    "Allocations has no 'Person' property — using the people property "
                    "%r instead. Rename it back to 'Person' in Notion.", name)
    except Exception:
        logging.exception("Could not read the Allocations schema; assuming 'Person'.")
    with _alloc_person_lock:
        _alloc_person_cache.update(at=now, name=name)
    return name


def alloc_rows(date_from: str, date_to: str, person_id: str | None = None) -> list[dict]:
    """Every allocation in [date_from, date_to] as flat records:
    {person_id, person_name, project_id, project_name, date, hours}.

    Allocations are day-dated (the property is still called "Week" for
    historical reasons). One flat read feeds both schedule views: the day
    planner groups by date, the weeks rollup buckets each date into its Monday
    with monday_of(). Rows predating the day-first planner sit on a Monday and
    simply read as hours on that Monday.

    person_name is only what the people property carries (usually nothing —
    see _person_name_map), so callers with a roster to hand should prefer it.
    """
    pname = _project_name_map()
    person_prop = alloc_person_prop()
    out: list[dict] = []
    kwargs = {"data_source_id": ALLOC_DS, "page_size": 100, "filter": {"and": [
        {"property": "Week", "date": {"on_or_after": date_from}},
        {"property": "Week", "date": {"on_or_before": date_to}},
    ]}}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            people = props.get(person_prop, {}).get("people") or []
            pid = people[0]["id"] if people else None
            if person_id and pid != person_id:
                continue
            rel = props["Project"]["relation"]
            if not rel or not props["Week"]["date"]:
                continue
            out.append({
                "person_id": pid,
                "person_name": people[0].get("name", "?") if people else "(unassigned)",
                "project_id": rel[0]["id"],
                "project_name": pname.get(rel[0]["id"], "(none)"),
                "date": props["Week"]["date"]["start"][:10],
                "hours": props["Hours"]["number"] or 0,
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return out


def set_project_member(project_id: str, person_id: str, add: bool) -> None:
    """Add or remove person_id from a project's People property. Idempotent:
    adding an existing member or removing a non-member is a no-op write."""
    page = _notion.pages.retrieve(page_id=project_id)
    members = [p["id"] for p in page["properties"].get("People", {}).get("people", [])]
    if add:
        if person_id in members:
            return
        members.append(person_id)
    else:
        if person_id not in members:
            return
        members.remove(person_id)
    _notion.pages.update(page_id=project_id, properties={"People": {"people": [{"id": m} for m in members]}})


def set_allocation_range(person_id: str, project_id: str, date_from: str, date_to: str,
                         hours: float) -> dict:
    """Upsert the same hours onto the (person, project) pair for every weekday
    in [date_from, date_to]. 0 hours deletes those days.

    Allocations are day-first: each day is an exact-date upsert and other days
    are untouched, so several projects can share one day — the whole point of
    the planner. A single-day save is just date_from == date_to.

    Weekends are skipped (nobody is scheduled on them). Takes _write_lock once
    for the whole range rather than per day, so a "repeat through Friday" is
    one burst instead of five racing upserts.
    """
    start = dt.date.fromisoformat(date_from)
    end = dt.date.fromisoformat(date_to)
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    with _write_lock:
        for iso in days:
            _set_allocation_locked(person_id, project_id, iso, hours)
    return {"ok": True, "hours": hours, "days": days}


MAX_COPY_ROWS = 500  # a copy is one write per booking — keep it a burst, not a job


def copy_week_allocations(from_monday: str, to_monday: str,
                          person_ids: list[str] | None = None,
                          project_id: str | None = None) -> dict:
    """Duplicate one week's bookings onto another week, weekday for weekday.

    Additive, not a replace: every (person, project, day) pair in the source
    week is written onto the matching weekday of the target week — overwriting
    that pair's hours if it was already booked there — and anything else
    already in the target week is left alone. Planning next week from last week
    is the point; quietly wiping edits already made to next week is not.

    person_ids / project_id narrow the copy to whatever the planner is filtered
    to, so the button copies what you can actually see. Takes _write_lock once
    for the whole week, like set_allocation_range.
    """
    src = dt.date.fromisoformat(from_monday)
    offset = (dt.date.fromisoformat(to_monday) - src).days
    pick = set(person_ids or [])
    plan: dict[tuple, float] = {}
    for a in alloc_rows(from_monday, (src + dt.timedelta(days=4)).isoformat()):
        if not a["person_id"] or not a["hours"]:
            continue
        if pick and a["person_id"] not in pick:
            continue
        if project_id and a["project_id"] != project_id:
            continue
        day = dt.date.fromisoformat(a["date"]) + dt.timedelta(days=offset)
        if day.weekday() >= 5:  # legacy week-scoped rows could land on a weekend
            continue
        key = (a["person_id"], a["project_id"], day.isoformat())
        plan[key] = plan.get(key, 0.0) + a["hours"]  # duplicate source rows fold into one write
    if len(plan) > MAX_COPY_ROWS:
        raise ValueError(f"that week has {len(plan)} bookings — more than the {MAX_COPY_ROWS} a copy will write")
    with _write_lock:
        for (pid, prid, iso), hours in plan.items():
            _set_allocation_locked(pid, prid, iso, hours)
    return {"ok": True, "copied": len(plan), "hours": round(sum(plan.values()), 2)}


def _set_allocation_locked(person_id: str, project_id: str, date_iso: str, hours: float) -> dict:
    person_prop = alloc_person_prop()
    matches = _query_all({
        "data_source_id": ALLOC_DS, "page_size": 100,
        "filter": {"and": [
            {"property": "Week", "date": {"equals": date_iso}},
            {"property": "Project", "relation": {"contains": project_id}},
            {"property": person_prop, "people": {"contains": person_id}},
        ]}})
    match = matches[0] if matches else None
    for extra in matches[1:]:  # duplicates from old races: fold into one
        _notion.pages.update(extra["id"], archived=True)
    if not hours:
        if match:
            _notion.pages.update(match["id"], archived=True)
        return {"ok": True, "hours": 0}
    if match:
        _notion.pages.update(match["id"], properties={"Hours": {"number": hours}})
    else:
        pmap = _project_name_map()
        _notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": ALLOC_DS},
            properties={
                "Allocation": {"title": [{"text": {"content": f"{pmap.get(project_id,'?')} — {date_iso}"}}]},
                person_prop: {"people": [{"id": person_id}]},
                "Project": {"relation": [{"id": project_id}]},
                "Week": {"date": {"start": date_iso}},
                "Hours": {"number": hours},
            })
    return {"ok": True, "hours": hours}


def planned_rows(date_from: str, date_to: str, person_id: str | None = None) -> list[dict]:
    """Allocation rows (person, project, hours) planned within the range.

    A row counts when its week's Monday falls in the range — whole-week,
    all-or-nothing, matching the pre-day-granularity behavior. Bucketing day
    rows to their Monday keeps 'scheduled' report totals identical whether a
    week was planned as one week cell or spread across days.
    """
    pname = _project_name_map()
    person_names = _person_name_map()
    person_prop = alloc_person_prop()
    out = []
    # day rows can sit up to 6 days after their Monday; filter query wide, bucket below
    query_to = (dt.date.fromisoformat(date_to) + dt.timedelta(days=6)).isoformat()
    kwargs = {"data_source_id": ALLOC_DS, "page_size": 100, "filter": {"and": [
        {"property": "Week", "date": {"on_or_after": date_from}},
        {"property": "Week", "date": {"on_or_before": query_to}},
    ]}}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            people = props.get(person_prop, {}).get("people") or []
            pid = people[0]["id"] if people else None
            if person_id and pid != person_id:
                continue
            rel = props["Project"]["relation"]
            if not rel or not props["Week"]["date"]:
                continue
            week_monday = monday_of(dt.date.fromisoformat(props["Week"]["date"]["start"][:10])).isoformat()
            if not (date_from <= week_monday <= date_to):
                continue
            out.append({
                "person_id": pid,
                "person": person_names.get(pid, "(unassigned)"),
                "project": pname.get(rel[0]["id"], "(none)"),
                "hours": props["Hours"]["number"] or 0,
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return out


# Serialize upserts: the query-then-create pattern would otherwise race and
# duplicate rows under overlapping saves (single-instance deploy, so this holds).
_write_lock = threading.Lock()


def _query_all(kwargs: dict) -> list:
    out = []
    while True:
        res = _notion.data_sources.query(**kwargs)
        out.extend(res["results"])
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return out


def set_entry_hours(entry_id: str, hours: float) -> dict:
    """Set one existing entry's Hours by page id. 0 removes the entry.

    Editing by page id (rather than the (person, project, date) upsert set_cell
    does) is what lets an admin correct a single row of a report without
    touching the day's other entries or their descriptions.

    The id comes from the client, so the page is retrieved and its parent
    checked first: a request may only ever touch Time Entries, never some other
    page the integration happens to have access to.
    """
    with _write_lock:
        page = _notion.pages.retrieve(entry_id)
        parent = page.get("parent") or {}
        if _bare(parent.get("data_source_id")) != _bare(TIME_DS):
            raise ValueError("not a time entry")
        if not hours:  # 0, None -> remove, like a blanked cell on the weekly grid
            _notion.pages.update(entry_id, archived=True)
            return {"ok": True, "hours": 0, "deleted": True}
        _notion.pages.update(entry_id, properties={"Hours": {"number": hours}})
        return {"ok": True, "hours": hours, "deleted": False}


def _bare(notion_id: str | None) -> str:
    """Notion ids compare equal with or without dashes (env vars carry either)."""
    return (notion_id or "").replace("-", "").lower()


def set_cell(person_id: str, project_id: str, date: str, hours: float) -> dict:
    """Upsert the (person, project, date) cell to `hours`. 0/None deletes the entry.

    Filters on Person in the query (not a Python scan), paginates, and
    consolidates duplicates: the grid shows one summed cell, so a save must
    leave exactly one row behind (or none for 0).
    """
    with _write_lock:
        matches = _query_all({
            "data_source_id": TIME_DS, "page_size": 100,
            "filter": {"and": [
                {"property": "Date", "date": {"equals": date}},
                {"property": "Project", "relation": {"contains": project_id}},
                {"property": "Person", "people": {"contains": person_id}},
            ]},
        })
        keep = matches[0] if matches else None
        for extra in matches[1:]:  # duplicates from old races/forms: fold into one
            _notion.pages.update(extra["id"], archived=True)

        if not hours:  # 0, None -> remove
            if keep:
                _notion.pages.update(keep["id"], archived=True)
            return {"ok": True, "hours": 0}

        if keep:
            _notion.pages.update(keep["id"], properties={
                "Hours": {"number": hours},
                "Person": {"people": [{"id": person_id}]},
            })
        else:
            create_entry(person_id, project_id, date, hours)
        return {"ok": True, "hours": hours}


# ---- Notion tickets ----------------------------------------------------
#
# An entry can optionally point at the Notion ticket the work was for. The two
# ways in need very different things from Notion:
#
#   * paste a link — needs nothing at all. A Notion URL carries the page id
#     *and* the title in its slug, so parse_task_url resolves it offline. That
#     works for every page in the workspace, shared with us or not.
#   * search — needs the Hours Tracker integration to actually read the ticket
#     boards (an admin adds the connection to the top-level pages; access
#     inherits to every child). Until that happens task_sources() is empty and
#     the picker degrades to paste-only rather than erroring.
#
# Search is scoped to the asking person by the ticket's *assignee*: their
# Notion user id is the same id a people property returns, and their email
# covers boards that keep the assignee in an email column instead.

_TASK_PROP = "Task"
_TASK_URL_PROP = "Task URL"

# What a ticket board might call its assignee column, best match first.
_ASSIGNEE_NAMES = ("assignee", "assigned to", "assigned", "owner", "responsible",
                   "person", "people", "developer")

# Our own databases are data sources too — never offer them as ticket boards,
# and never accept one of their rows as a "ticket". Both id flavours are listed
# because a page's parent is reported as a data source or as a database
# depending on how it was created.
_OWN_DS = {_bare(v) for k, v in _ids.items()
           if v and (k.endswith("_ds_id") or k.endswith("_db_id"))}

_TASK_SRC_TTL = 300.0
_task_src_cache: dict = {"at": 0.0, "sources": None}
_task_schema_cache: dict = {}   # ds id -> (fetched_at, {title, people, email})

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_ID_TAIL = re.compile(r"^(.*?)-?([0-9a-fA-F]{32})$")


def parse_task_url(url: str) -> dict | None:
    """A pasted Notion URL -> {"id", "url", "label"} without calling Notion.

    Notion links carry the page id as 32 hex at the end of the last path
    segment and the page title as the slug before it, so a paste resolves with
    no permission on the page whatsoever. Returns None for anything that isn't
    a link to a Notion *page*.
    """
    url = (url or "").strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    try:
        u = urlparse(url)
    except ValueError:
        return None
    # notion.com is what Notion hands out today (app.notion.com/p/<slug>-<id>);
    # notion.so is the older domain still in everyone's bookmarks, and
    # notion.site covers a published page. Matched as whole labels, not by
    # suffix: a plain endswith would also welcome evilnotion.com, and these
    # links get clicked out of client-facing reports.
    host = (u.netloc or "").lower().split("@")[-1].split(":")[0].rstrip(".")
    if not any(host == d or host.endswith("." + d)
               for d in ("notion.com", "notion.so", "notion.site")):
        return None

    q = parse_qs(u.query)
    if "v" in q and "p" not in q:
        # ?v=<view id> is a board link, and its trailing id is the database's —
        # accepting it would file hours against a whole board.
        return None

    seg = unquote((u.path or "").rstrip("/").split("/")[-1])
    page_id, label = None, ""
    if "p" in q:
        # Side-peek link: the ticket is in ?p=, and the slug in the path
        # belongs to the *board*, so it must not be used as the label.
        cand = q["p"][0].replace("-", "").lower()
        page_id = cand if _HEX32.match(cand) else None
    if not page_id:
        # Anchor at the end of the segment: searching it loosely mis-slices the
        # id when the slug itself ends in hex-ish text.
        cand = seg.replace("-", "").lower()[-32:]
        if _HEX32.match(cand):
            page_id = cand
            m = _ID_TAIL.match(seg)
            if m:
                label = m.group(1).replace("-", " ").strip()
    if not page_id:
        return None
    return {"id": page_id, "url": url.split("#")[0], "label": label[:200]}


def task_sources() -> list[str]:
    """The ticket boards to search: TASKS_DS_IDS if set, else everything the
    integration can see minus our own four. Empty until a board is connected."""
    now = time.time()
    if _task_src_cache["sources"] is not None and now - _task_src_cache["at"] < _TASK_SRC_TTL:
        return _task_src_cache["sources"]
    named = [s.strip() for s in os.environ.get("TASKS_DS_IDS", "").split(",") if s.strip()]
    if named:
        # Naming the boards outright skips discovery entirely — which also makes
        # this work the moment a board is connected, rather than whenever
        # Notion's search index catches up with it.
        _task_src_cache.update(at=now, sources=named)
        return named
    out: list[str] = []
    try:
        cursor = None
        while True:
            kwargs = {"filter": {"property": "object", "value": "data_source"}, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            res = _notion.search(**kwargs)
            for o in res["results"]:
                if _bare(o["id"]) in _OWN_DS:
                    continue
                out.append(o["id"])
            if not res.get("has_more"):
                break
            cursor = res["next_cursor"]
    except Exception:
        logging.exception("Listing ticket boards failed — ticket search stays off")
        out = []
    _task_src_cache.update(at=now, sources=out)
    return out


def _task_schema(ds_id: str) -> dict:
    """Which property on a ticket board is its title, and which its assignee.

    Ticket boards belong to other teams and get renamed freely, so nothing is
    addressed by a hardcoded name here — the lesson from alloc_person_prop.
    """
    now = time.time()
    hit = _task_schema_cache.get(ds_id)
    if hit and now - hit[0] < _TASK_SRC_TTL:
        return hit[1]
    info = {"title": None, "people": None, "email": None}
    try:
        props = _notion.data_sources.retrieve(ds_id)["properties"]
    except Exception:
        _task_schema_cache[ds_id] = (now, info)
        return info

    def rank(name: str) -> int:
        low = name.lower()
        for i, want in enumerate(_ASSIGNEE_NAMES):
            if want in low:
                return i
        return len(_ASSIGNEE_NAMES)

    for name, p in props.items():
        if p.get("type") == "title":
            info["title"] = name
    people = [n for n, p in props.items() if p.get("type") == "people"]
    emails = [n for n, p in props.items() if p.get("type") == "email"]
    if people:
        info["people"] = sorted(people, key=rank)[0]
    if emails:
        info["email"] = sorted(emails, key=rank)[0]
    _task_schema_cache[ds_id] = (now, info)
    return info


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop["title"]).strip()
    return ""


def _assigned_to(page: dict, person_id: str | None, email: str | None) -> bool:
    """Is this ticket assigned to the asking person? A people property matches
    on Notion user id (the very id they logged in with); an email or text
    property matches on address, so boards without a people column work too."""
    want = (email or "").strip().lower()
    for prop in (page.get("properties") or {}).values():
        kind = prop.get("type")
        if kind == "people" and person_id:
            if any(_bare(u.get("id")) == _bare(person_id) for u in prop.get("people") or []):
                return True
        elif kind == "email" and want:
            if (prop.get("email") or "").strip().lower() == want:
                return True
        elif kind == "rich_text" and want:
            text = "".join(t.get("plain_text", "") for t in prop.get("rich_text") or [])
            if want in text.lower():
                return True
    return False


def _task_result(page: dict, person_id: str | None, email: str | None) -> dict:
    return {
        "id": page["id"],
        "title": _page_title(page) or "(untitled)",
        "url": page.get("url") or f"https://www.notion.so/{_bare(page['id'])}",
        "mine": _assigned_to(page, person_id, email),
    }


_MY_TASKS_TTL = 60.0
_MAX_BOARDS_QUERIED = 12
_my_tasks_cache: dict = {}   # person key -> (fetched_at, results)


def my_tasks(person_id: str | None, email: str | None, limit: int = 8) -> list[dict]:
    """The picker's opening list: tickets assigned to this person, freshest
    first. One query per board, filtered on that board's own assignee property,
    so the list is short and personal before anyone types a thing.

    Only boards that actually get *queried* count against the fan-out cap.
    Capping the raw board list instead would spend the budget on boards with no
    assignee column at all — with 27 connected boards and 11 usable ones, that
    left 8 of the 11 unread. Results are cached briefly because this runs when
    the field is focused, and a dozen sequential board queries is a slow way to
    open a dropdown twice.
    """
    key = f"{person_id}|{(email or '').lower()}|{limit}"
    now = time.time()
    hit = _my_tasks_cache.get(key)
    if hit and now - hit[0] < _MY_TASKS_TTL:
        return hit[1]

    out: list[dict] = []
    queried = 0
    for ds_id in task_sources():
        if queried >= _MAX_BOARDS_QUERIED or len(out) >= limit:
            break
        schema = _task_schema(ds_id)
        if schema["people"] and person_id:
            flt = {"property": schema["people"], "people": {"contains": person_id}}
        elif schema["email"] and email:
            flt = {"property": schema["email"], "email": {"equals": email}}
        else:
            continue          # no assignee column: nothing to scope by, not a query
        queried += 1
        try:
            res = _notion.data_sources.query(
                data_source_id=ds_id, page_size=limit, filter=flt,
                sorts=[{"timestamp": "last_edited_time", "direction": "descending"}])
        except Exception:
            logging.warning("Ticket board %s wouldn't answer an assignee query", ds_id)
            continue
        out.extend(_task_result(p, person_id, email) for p in res["results"])
    out = out[:limit]
    _my_tasks_cache[key] = (now, out)
    return out


def search_tasks(query: str, person_id: str | None, email: str | None,
                 mine_only: bool = False, limit: int = 10) -> list[dict]:
    """Tickets whose title matches `query`, the asking person's own first.

    Notion's search endpoint is the right tool precisely because the tickets
    are scattered: it is workspace-wide and matches titles. A result is kept
    only if it is a row on a known ticket board — that is what separates a
    ticket from a loose document that happens to match.
    """
    query = (query or "").strip()
    if not query:
        return my_tasks(person_id, email, limit)
    sources = {_bare(s) for s in task_sources()}
    if not sources:
        return []
    try:
        res = _notion.search(query=query, page_size=50,
                             filter={"property": "object", "value": "page"})
    except Exception:
        logging.exception("Ticket search failed")
        return []
    out = []
    for page in res["results"]:
        parent = page.get("parent") or {}
        if parent.get("type") != "data_source_id":
            continue
        if _bare(parent.get("data_source_id")) not in sources:
            continue
        out.append(_task_result(page, person_id, email))
    if mine_only:
        out = [t for t in out if t["mine"]]
    out.sort(key=lambda t: (not t["mine"], t["title"].lower()))
    return out[:limit]


def resolve_task(page_id: str) -> dict | None:
    """Confirm a pasted ticket and read its real title. None when the page
    isn't shared with the integration — the caller then keeps the URL and the
    label parsed from the slug, so pasting works with no access at all.

    A page from one of our own databases comes back flagged `ours`: pasting a
    time entry or a project row as the "ticket" for some hours is never what
    anyone meant, and it reads perfectly plausibly once stored.
    """
    try:
        page = _notion.pages.retrieve(page_id)
    except Exception:
        return None
    parent = page.get("parent") or {}
    ours = _bare(parent.get("data_source_id") or parent.get("database_id") or "") in _OWN_DS
    return {"id": page["id"], "title": _page_title(page), "ours": ours,
            "url": page.get("url") or f"https://www.notion.so/{_bare(page_id)}"}


# ---- invoices ----------------------------------------------------------
#
# An invoice records a decision *about* logged hours — "for July we billed
# Auter 138 of the 142 hours tracked" — and never rewrites a Time Entry. That
# keeps the three layers distinct: set_entry_hours fixes what was *logged*, the
# export screen adjusts what a client *sees* for one send, and this records
# what was *billed*.

INVOICES_DS = _ids.get("invoices_ds_id")   # optional: unset until set up


def invoices_enabled() -> bool:
    return bool(INVOICES_DS)


def ensure_invoice_properties() -> None:
    """Make sure the Invoices db can hold the per-entry adjustments.

    Invoices created before the detail view only stored totals, so add the
    column on startup the way ensure_person_property does.
    """
    if not INVOICES_DS:
        return
    ds = _notion.data_sources.retrieve(INVOICES_DS)
    if _ADJ_PROP not in ds["properties"]:
        _notion.data_sources.update(INVOICES_DS, properties={_ADJ_PROP: {"rich_text": {}}})


# Only the entries whose billed hours differ from what was logged are stored —
# usually a handful — so reopening an invoice can show exactly what was billed
# without keeping a copy of every line. A month of overrides can still outgrow
# one rich-text object, hence the chunking.
_ADJ_PROP = "Adjustments"
_CHUNK = 1900


def _adjustments_to_rich_text(adjustments: dict) -> list:
    if not adjustments:
        return []
    blob = json.dumps({_bare(k): v for k, v in adjustments.items()}, separators=(",", ":"))
    chunks = [blob[i:i + _CHUNK] for i in range(0, len(blob), _CHUNK)]
    if len(chunks) > 100:                     # Notion caps a rich_text array at 100
        logging.warning("Invoice adjustments too large to store (%d chars)", len(blob))
        return []
    return [{"text": {"content": c}} for c in chunks]


def _adjustments_from_props(props: dict) -> dict:
    text = "".join(t.get("plain_text", "") for t in props.get(_ADJ_PROP, {}).get("rich_text") or [])
    if not text:
        return {}
    try:
        return {k: float(v) for k, v in json.loads(text).items()}
    except (ValueError, TypeError, AttributeError):
        logging.warning("Invoice adjustments weren't readable JSON")
        return {}


def _invoice_row(page: dict, pname: dict, people: dict) -> dict:
    props = page["properties"]
    rel = props.get("Project", {}).get("relation") or []
    pid = rel[0]["id"] if rel else None
    month = (props.get("Month", {}).get("date") or {}).get("start") or ""
    issued = (props.get("Issued", {}).get("date") or {}).get("start") or ""
    saved = props.get("Saved by", {}).get("people") or []
    note = props.get("Note", {}).get("rich_text") or []
    return {
        "id": page["id"],
        "project_id": pid,
        "project": pname.get(pid, "(unknown project)") if pid else "(none)",
        "month": month[:10],
        "hours_tracked": props.get("Hours tracked", {}).get("number") or 0,
        "hours_billed": props.get("Hours billed", {}).get("number") or 0,
        "issued": issued[:10],
        # people properties come back nameless, so resolve against the roster
        "saved_by": people.get(saved[0]["id"], "") if saved else "",
        "note": "".join(t.get("plain_text", "") for t in note),
        "adjustments": _adjustments_from_props(props),
        "url": page.get("url", ""),
    }


def find_invoice(project_id: str, month: str) -> dict | None:
    """The invoice already filed for this project and month, if any."""
    if not INVOICES_DS:
        return None
    res = _notion.data_sources.query(data_source_id=INVOICES_DS, page_size=2, filter={"and": [
        {"property": "Project", "relation": {"contains": project_id}},
        {"property": "Month", "date": {"equals": month}},
    ]})
    if not res["results"]:
        return None
    return _invoice_row(res["results"][0], _project_name_map(), _person_name_map())


def list_invoices(project_id: str | None = None, limit: int = 200) -> list[dict]:
    """Every invoice, newest month first, optionally for one project."""
    if not INVOICES_DS:
        return []
    kwargs = {"data_source_id": INVOICES_DS, "page_size": 100,
              "sorts": [{"property": "Month", "direction": "descending"}]}
    if project_id:
        kwargs["filter"] = {"property": "Project", "relation": {"contains": project_id}}
    pname, people = _project_name_map(), _person_name_map()
    out = []
    for page in _query_all(kwargs):
        out.append(_invoice_row(page, pname, people))
        if len(out) >= limit:
            break
    return out


def get_invoice(invoice_id: str) -> dict | None:
    """One invoice by page id, refusing anything that isn't one.

    The id arrives from a URL, so the parent is checked the same way
    set_entry_hours checks a time entry's.
    """
    if not INVOICES_DS:
        return None
    try:
        page = _notion.pages.retrieve(invoice_id)
    except Exception:
        return None
    parent = page.get("parent") or {}
    if _bare(parent.get("data_source_id") or "") != _bare(INVOICES_DS):
        return None
    return _invoice_row(page, _project_name_map(), _person_name_map())


def save_invoice(project_id: str, month: str, hours_tracked: float, hours_billed: float,
                 issued: str, note: str = "", person_id: str | None = None,
                 adjustments: dict | None = None) -> dict:
    """File (or correct) the invoice for one project and one month.

    Upserts on (project, month), the same discipline as set_cell: saving July
    for a project twice is nearly always a correction, not a second bill. On a
    correction `Issued` keeps whatever it already said unless a new date is
    passed — Notion's own page history is the audit trail.
    """
    if not INVOICES_DS:
        raise RuntimeError("Invoices database isn't configured (INVOICES_DS_ID).")
    pname = _project_name_map()
    label = pname.get(project_id, "Project")
    title = f"{label} — {dt.date.fromisoformat(month).strftime('%B %Y')}"
    props = {
        "Invoice": {"title": [{"text": {"content": title}}]},
        "Project": {"relation": [{"id": project_id}]},
        "Month": {"date": {"start": month}},
        "Hours tracked": {"number": round(hours_tracked, 2)},
        "Hours billed": {"number": round(hours_billed, 2)},
        "Note": {"rich_text": [{"text": {"content": note[:1900]}}]},
        # rewritten every save, so an entry billed back at its logged hours
        # stops being an adjustment instead of lingering as one
        _ADJ_PROP: {"rich_text": _adjustments_to_rich_text(adjustments or {})},
    }
    if issued:
        props["Issued"] = {"date": {"start": issued}}
    if person_id:
        props["Saved by"] = {"people": [{"id": person_id}]}

    with _write_lock:
        matches = _query_all({
            "data_source_id": INVOICES_DS, "page_size": 100,
            "filter": {"and": [
                {"property": "Project", "relation": {"contains": project_id}},
                {"property": "Month", "date": {"equals": month}},
            ]},
        })
        keep = matches[0] if matches else None
        for extra in matches[1:]:   # duplicates from an old race: fold into one
            _notion.pages.update(extra["id"], archived=True)
        if keep:
            _notion.pages.update(keep["id"], properties=props)
            page = _notion.pages.retrieve(keep["id"])
        else:
            page = _notion.pages.create(
                parent={"type": "data_source_id", "data_source_id": INVOICES_DS},
                properties=props)
    return dict(_invoice_row(page, pname, _person_name_map()), replaced=bool(keep))


def entry_task(props: dict) -> dict:
    """The ticket on a Time Entries row, read tolerantly: both properties are
    optional (every entry logged before this feature has none) and a rename in
    the Notion UI must not take a report down."""
    url = props.get(_TASK_URL_PROP, {}).get("url") or ""
    text = props.get(_TASK_PROP, {}).get("rich_text") or []
    label = "".join(t.get("plain_text", "") for t in text).strip()
    return {"task_url": url, "task": label or ("Notion ticket" if url else "")}
