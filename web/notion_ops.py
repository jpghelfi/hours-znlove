"""Notion data operations for the web app.

Notion remains the source of truth. This module reads people/projects and
reads/writes Time Entries via the 2025-09-03 data-source API.
"""
from __future__ import annotations

import datetime as dt
import logging
import sys
import threading
import time
from pathlib import Path

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
                "person_id": pid, "person": person,
                "project": pname.get(rel[0]["id"], "(none)") if rel else "(none)",
                "date": date["start"][:10] if date else None,
                "hours": props["Hours"]["number"] or 0,
                "description": desc[0]["plain_text"] if desc else "",
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return [e for e in out if e["date"]]


# ---- writes ------------------------------------------------------------

def create_entry(person_id: str | None, project_id: str, date: str, hours: float, description: str = "") -> None:
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


def schedule_grid(start_monday: dt.date, n_weeks: int = 6, person_id: str | None = None) -> dict:
    """Allocations grid: rows = person × project, columns = n_weeks Mondays.

    Allocation rows may carry any weekday date (day view writes exact days);
    each row is bucketed into its week's Monday column, so week cells show the
    week's sum regardless of how granular the underlying plan is.
    """
    weeks = [(start_monday + dt.timedelta(weeks=i)).isoformat() for i in range(n_weeks)]
    last_day = (start_monday + dt.timedelta(weeks=n_weeks - 1, days=6)).isoformat()
    return _alloc_grid(weeks, weeks[0], last_day,
                       lambda iso: monday_of(dt.date.fromisoformat(iso)).isoformat(),
                       person_id)


def schedule_day_grid(monday: dt.date, person_id: str | None = None) -> dict:
    """One week of allocations at day granularity: columns = Mon–Fri.

    Week-view edits consolidate a pair's plan onto the Monday, so hours
    planned per-week show up in Monday's cell until they're spread out here.
    """
    days = [(monday + dt.timedelta(days=i)).isoformat() for i in range(5)]
    return _alloc_grid(days, days[0], days[-1], lambda iso: iso, person_id)


def _alloc_grid(cols: list[str], range_from: str, range_to: str, bucket, person_id: str | None) -> dict:
    """Shared allocations grid: rows = person × project, columns = cols.
    bucket maps a row's date iso to its column iso (unknown columns are dropped)."""
    pname = _project_name_map()
    rows: dict = {}
    kwargs = {"data_source_id": ALLOC_DS, "page_size": 100, "filter": {"and": [
        {"property": "Week", "date": {"on_or_after": range_from}},
        {"property": "Week", "date": {"on_or_before": range_to}},
    ]}}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            people = props["Person"]["people"]
            pid = people[0]["id"] if people else None
            pname_person = people[0].get("name", "?") if people else "(unassigned)"
            if person_id and pid != person_id:
                continue
            rel = props["Project"]["relation"]
            if not rel or not props["Week"]["date"]:
                continue
            col = bucket(props["Week"]["date"]["start"][:10])
            if col not in cols:
                continue
            key = (pid, rel[0]["id"])
            r = rows.setdefault(key, {
                "person_id": pid, "person_name": pname_person,
                "project_id": rel[0]["id"], "project_name": pname.get(rel[0]["id"], "(none)"),
                "cells": {c: 0.0 for c in cols},
            })
            r["cells"][col] += props["Hours"]["number"] or 0
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]

    ordered = sorted(rows.values(), key=lambda r: (r["person_name"].lower(), r["project_name"].lower()))
    # per-person totals per column for the capacity heat map
    people_totals: dict = {}
    for r in ordered:
        pt = people_totals.setdefault(r["person_name"], {c: 0.0 for c in cols})
        for c in cols:
            pt[c] += r["cells"][c]
    return {"weeks": cols, "rows": ordered, "people_totals": people_totals}


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


def set_allocation(person_id: str, project_id: str, date_iso: str, hours: float,
                   scope: str = "week") -> dict:
    """Upsert the (person, project) allocation for a week or a single day.
    0 deletes it.

    scope="week": date_iso is the Monday; the pair's whole week (any day-dated
    rows included) is replaced by one Monday-dated row, so a week-cell edit is
    authoritative for that week.
    scope="day": exact-date upsert; other days of the week are untouched.
    """
    with _write_lock:
        return _set_allocation_locked(person_id, project_id, date_iso, hours, scope)


def _set_allocation_locked(person_id: str, project_id: str, date_iso: str, hours: float,
                           scope: str) -> dict:
    if scope == "week":
        sunday = (dt.date.fromisoformat(date_iso) + dt.timedelta(days=6)).isoformat()
        date_filter = [{"property": "Week", "date": {"on_or_after": date_iso}},
                       {"property": "Week", "date": {"on_or_before": sunday}}]
    else:
        date_filter = [{"property": "Week", "date": {"equals": date_iso}}]
    matches = _query_all({
        "data_source_id": ALLOC_DS, "page_size": 100,
        "filter": {"and": date_filter + [
            {"property": "Project", "relation": {"contains": project_id}},
            {"property": "Person", "people": {"contains": person_id}},
        ]}})
    match = matches[0] if matches else None
    for extra in matches[1:]:
        _notion.pages.update(extra["id"], archived=True)
    if not hours:
        if match:
            _notion.pages.update(match["id"], archived=True)
        return {"ok": True, "hours": 0}
    if match:
        # week scope may keep a day-dated row — re-date it to the Monday too
        _notion.pages.update(match["id"], properties={
            "Hours": {"number": hours},
            "Week": {"date": {"start": date_iso}},
        })
    else:
        pmap = _project_name_map()
        _notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": ALLOC_DS},
            properties={
                "Allocation": {"title": [{"text": {"content": f"{pmap.get(project_id,'?')} — {date_iso}"}}]},
                "Person": {"people": [{"id": person_id}]},
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
            people = props["Person"]["people"]
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
                "person": people[0].get("name", "(unassigned)") if people else "(unassigned)",
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
