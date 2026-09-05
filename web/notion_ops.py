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
    id in the People property), for the schedule page's assignment view.

    Every project also carries "budget" — its monthly hour budget dict, or None
    when it isn't budgeted — and "pm_id"/"am_id", the Notion user id of its PM
    and Account manager (or None) — plus "active", which matters to the callers
    that pass active_only=False and still need to tell the two apart (the
    invoices page lists every project but only offers active ones to invoice)
    — and "partner"/"umbrella", the client umbrella this project sits under
    ("" when the client is direct) and whether this row *is* that umbrella —
    and "billing", the rate, currency and client contact an invoice PDF is
    addressed with.
    All of them are parsed off the rows this query already returns, so none of
    them costs an extra call anywhere it's wanted."""
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
            project = {"id": row["id"], "name": name, "active": active,
                       "budget": _budget_from_props(props),
                       "billing": _billing_from_props(props),
                       "pm_id": _role_from_props(props, "pm"),
                       "am_id": _role_from_props(props, "am"),
                       "partner": _partner_from_props(props),
                       "umbrella": bool(props.get(UMBRELLA_PROP, {}).get("checkbox"))}
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
    """All entries in [date_from, date_to] (ISO dates), optionally for one person.

    `person_id` filters in **Python, on purpose** — pushing it into the Notion
    query as `{"property": "Person", "people": {"contains": …}}` (the way
    set_cell does) would look strictly cheaper and would quietly drop every
    entry submitted through Notion's own form, which carries `Logged by` and no
    `Person` at all. _row_person reads both; a server-side filter can only see
    one.
    """
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
                **entry_goal(props),
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
                **entry_goal(props),
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return out


# ---- writes ------------------------------------------------------------

class NoteRequired(Exception):
    """A new entry arrived with neither a description nor a ticket."""

    def __init__(self, message: str = ""):
        super().__init__(message or (
            f"Say what you worked on — at least {MIN_DESCRIPTION} characters — "
            "or link the Notion ticket."))


# How much description counts as one. A floor stops *blank*, not *lazy* — five
# characters and ten both let "aaaaaaaaaa" through, and neither can tell.
#
# So it is set where it costs the least in false refusals: measured over July
# and August, ten would have rejected "ZN-999", "TB-54", "PR Review", "banners"
# and "Text PDP" — descriptions that name the work better than most sentences —
# while five rejects "pm" and lets the rest stand. The ticket link remains the
# better record for anything with a ticket, and satisfies this on its own.
MIN_DESCRIPTION = 5


def note_ok(description: str = "", task_url: str = "") -> bool:
    """Does this entry say what it was for?

    Either arm satisfies it. Whitespace doesn't count toward the length — an
    entry described as eleven spaces is a blank one.
    """
    if (task_url or "").strip():
        return True
    return len(" ".join((description or "").split())) >= MIN_DESCRIPTION


def require_note(description: str = "", task_url: str = "") -> None:
    """Raise unless the entry says what it was for."""
    if not note_ok(description, task_url):
        raise NoteRequired()


def create_entry(person_id: str | None, project_id: str, date: str, hours: float,
                 description: str = "", task_url: str = "", task_label: str = "",
                 enforce: bool = False, note: bool = False) -> None:
    """Write one new time entry.

    `enforce` opts this write into the project's monthly budget cap (see
    check_budget). It defaults to off so every existing caller — set_cell, the
    CLIs, the Harvest importer — keeps working exactly as before, and only the
    routes that serve a non-admin turn it on. A new row adds all of its hours,
    so the delta is simply `hours`.

    `note` opts it into "say what this was for" (see require_note), and defaults
    to off for the same reason and with more force: `src/sync_harvest.py` writes
    `"Harvest"` as the description whenever a Harvest entry has no notes, so
    enforcing here would make the importer refuse hours somebody genuinely
    worked. docs/budgets.md settled that trade once already — a checker that
    corrupts the record to protect a rule is the wrong checker. Only the two
    routes that serve a person typing into a browser turn it on.

    Checked before the budget: it costs no Notion call, and being told "add a
    description" is a better first answer than "this project is over budget"
    when both are true.

    When enforcing, the check and the write happen under `_write_lock`
    together. Checking outside it is a time-of-check/time-of-use race: two
    people submitting 1 h each against a project sitting at 39 h of a 40 h cap
    would both read 39, both compute 40, both pass, and the month would end at
    41 — past a cap that is supposed to be a hard stop. set_cell already
    serializes for exactly this reason.

    **`_write_lock` is not reentrant**, so this only ever takes it on the
    enforce path — set_cell calls this from *inside* its own lock, and does so
    with `enforce=False` (it has already run the check itself, against the
    delta rather than the raw hours). Keep it that way or this deadlocks.
    """
    if note:
        require_note(description, task_url)
    pname_map = _project_name_map()   # a Notion read: do it before any lock
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

    def _write():
        _notion.pages.create(parent={"type": "data_source_id", "data_source_id": TIME_DS},
                             properties=props)

    if not enforce:
        _write()
        return
    budget_for(project_id)      # warm the cache before taking the global lock
    with _write_lock:
        check_budget(project_id, date, hours)
        _write()


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
    {id, person_id, person_name, project_id, project_name, date, hours}.

    `id` is the Notion page id, so a caller that wants to delete exactly the
    rows it just read (clear_week_allocations) doesn't have to re-derive them
    through the (person, project, day) upsert key.

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
    where = [
        {"property": "Week", "date": {"on_or_after": date_from}},
        {"property": "Week", "date": {"on_or_before": date_to}},
    ]
    if person_id:
        # One person is asked for on every non-admin view of /schedule, so it
        # goes into the query rather than being paged through and dropped in
        # Python. Same result — an unassigned row can't contain the id either
        # — for a fraction of the rows fetched.
        where.append({"property": person_prop, "people": {"contains": person_id}})
    kwargs = {"data_source_id": ALLOC_DS, "page_size": 100, "filter": {"and": where}}
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
                "id": row["id"],
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
                          project_id: str | None = None,
                          project_ids: list[str] | None = None) -> dict:
    """Duplicate one week's bookings onto another week, weekday for weekday.

    Additive, not a replace: every (person, project, day) pair in the source
    week is written onto the matching weekday of the target week — overwriting
    that pair's hours if it was already booked there — and anything else
    already in the target week is left alone. Planning next week from last week
    is the point; quietly wiping edits already made to next week is not.

    person_ids / project_id / project_ids narrow the copy to whatever the
    planner is filtered to, so the button copies what you can actually see —
    project_ids is how a *partner* filter arrives, resolved to its projects by
    the caller. Takes _write_lock once for the whole week, like
    set_allocation_range.
    """
    src = dt.date.fromisoformat(from_monday)
    offset = (dt.date.fromisoformat(to_monday) - src).days
    pick = set(person_ids or [])
    keep = set(project_ids) if project_ids is not None else None
    plan: dict[tuple, float] = {}
    for a in alloc_rows(from_monday, (src + dt.timedelta(days=4)).isoformat()):
        if not a["person_id"] or not a["hours"]:
            continue
        if pick and a["person_id"] not in pick:
            continue
        if project_id and a["project_id"] != project_id:
            continue
        if keep is not None and a["project_id"] not in keep:
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


def paste_allocations(items: list[dict], dates: list[str]) -> dict:
    """Write the same set of bookings onto each of several days.

    The copy counterpart to a drag, and it deliberately reads differently: a
    drag **adds** its hours where it lands, because those hours physically
    moved, while a paste **sets** the pairs it names. "Make Wednesday look like
    Monday" means Kepos 6h — not 8h because Wednesday already had 2h of it.

    Only the pairs being pasted are touched: everything else already booked on
    the target day survives. Replacing a whole day is Clear day followed by a
    paste, and both halves already exist.

    `items` is [{person_id, project_id, hours}, …] — fully resolved pairs, so
    this works the same whether the planner is grouped by person (the pills are
    projects, the target row supplies the person) or by project (the other way
    round). Resolving that is the caller's job; here a pair is a pair.

    `_write_lock` is taken **per day, not for the whole paste**: this can be
    hundreds of writes, and holding the lock across all of them would freeze
    every weekly-grid save in the app meanwhile. Each day is an independent
    upsert, so fairness costs nothing here.
    """
    days = []
    for iso in dates:
        d = dt.date.fromisoformat(iso)
        if d.weekday() < 5 and iso not in days:   # weekends are never planned
            days.append(iso)
    pairs = []
    for i in items:
        hours = round(float(i.get("hours") or 0), 2)
        if i.get("person_id") and i.get("project_id") and hours > 0:
            pairs.append((i["person_id"], i["project_id"], hours))
    if not pairs or not days:
        return {"ok": True, "written": 0, "cells": []}
    if len(pairs) * len(days) > MAX_COPY_ROWS:
        raise ValueError(f"that would write {len(pairs) * len(days)} bookings — more than "
                         f"the {MAX_COPY_ROWS} one paste will make")
    cells = []
    for iso in days:
        with _write_lock:
            for person_id, project_id, hours in pairs:
                _set_allocation_locked(person_id, project_id, iso, hours)
                cells.append({"date": iso, "person_id": person_id,
                              "project_id": project_id, "hours": hours})
    return {"ok": True, "written": len(cells), "cells": cells}


def clear_allocations(date_from: str, date_to: str, person_ids: list[str] | None = None,
                      project_id: str | None = None,
                      project_ids: list[str] | None = None) -> dict:
    """Delete every booking in [date_from, date_to] that the given filters keep.

    One function behind all three "wipe what I can see" buttons — a whole week,
    one day column, one day cell — because they differ only in how narrow the
    range and the filters are: a cell is a single day scoped to its own row.

    Scoped to whatever the planner is filtered to (person_ids / project_id /
    project_ids — the last being a partner filter, resolved to its projects by
    the caller), so a button removes exactly the bookings on screen and nothing
    behind a filter. That is load-bearing rather than tidy: a partner filter the
    delete could not see would wipe the whole company's week from a screen
    showing one partner's. Deletes by page id — the rows this same read returned — rather than
    re-deriving (person, project, day) keys, so a duplicate row left over from
    an old race goes too instead of surviving as a ghost.

    Notion has no bulk archive, so this is one write per booking under a single
    _write_lock, capped like a copy — checked before the first write, so a
    too-big range is refused whole rather than half-deleted.
    """
    pick = set(person_ids or [])
    keep = set(project_ids) if project_ids is not None else None
    doomed = []
    for a in alloc_rows(date_from, date_to):
        if pick and a["person_id"] not in pick:
            continue
        if project_id and a["project_id"] != project_id:
            continue
        if keep is not None and a["project_id"] not in keep:
            continue
        doomed.append(a)
    if len(doomed) > MAX_COPY_ROWS:
        raise ValueError(f"that range has {len(doomed)} bookings — more than the {MAX_COPY_ROWS} a clear will delete")
    with _write_lock:
        for a in doomed:
            _notion.pages.update(a["id"], archived=True)
    return {"ok": True, "cleared": len(doomed),
            "hours": round(sum(a["hours"] for a in doomed), 2)}


def clear_week_allocations(monday: str, person_ids: list[str] | None = None,
                           project_id: str | None = None,
                           project_ids: list[str] | None = None) -> dict:
    """Delete a whole week of bookings in one go — Mon–Fri of `monday`, under
    the page's filters. A thin week-shaped wrapper over clear_allocations."""
    mon = dt.date.fromisoformat(monday)
    return clear_allocations(monday, (mon + dt.timedelta(days=4)).isoformat(),
                             person_ids, project_id, project_ids)


def move_allocation(person_id: str, project_id: str, date_iso: str,
                    to_person_id: str, to_project_id: str, to_date: str,
                    copy: bool = False) -> dict:
    """Drag one booking onto another day, another row, or both.

    The hours moved are re-read from Notion rather than taken from the browser:
    the pill on screen can be a fold of duplicate rows for one (person,
    project, day) pair, and it can be stale. Whatever is actually booked on the
    source day is what lands on the target.

    Landing on a day that already books the same pair **adds** to it — you
    dropped 3h of Kepos onto a day already holding 2h of Kepos, and 5h is the
    only reading of that gesture that doesn't silently lose hours.

    Move (the default) deletes the source afterwards; `copy` leaves it.

    The whole read-modify-write sits inside _write_lock, not just the two
    writes: the target's hours are read and then added to, so a read taken
    outside the lock could be overtaken by another drag landing on the same day
    and the second write would silently drop the first one's hours. Holding the
    lock across the reads costs two extra queries' worth of contention on a
    gesture that happens a few times a minute, and makes the sum honest.
    """
    def booked(pid: str, prid: str, day: str) -> float:
        return sum(a["hours"] for a in alloc_rows(day, day)
                   if a["person_id"] == pid and a["project_id"] == prid)

    with _write_lock:
        src_hours = booked(person_id, project_id, date_iso)
        if not src_hours:
            return {"ok": False, "error": "nothing booked there any more"}
        if (person_id, project_id, date_iso) == (to_person_id, to_project_id, to_date):
            return {"ok": True, "hours": src_hours, "from_hours": src_hours, "moved": 0}
        dst_hours = booked(to_person_id, to_project_id, to_date)
        total = round(src_hours + dst_hours, 2)
        if total > 24:
            return {"ok": False,
                    "error": "that would book more than 24h of one project in a day"}
        _set_allocation_locked(to_person_id, to_project_id, to_date, total)
        if not copy:
            _set_allocation_locked(person_id, project_id, date_iso, 0)
    return {"ok": True, "hours": total, "from_hours": src_hours if copy else 0,
            "moved": src_hours}


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
                # both the name (what pva() aggregates by) and the id (what a
                # role filter narrows by) — same reasoning as entries_between
                # carrying both "project" and "project_id"
                "project_id": rel[0]["id"],
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
        # The project and date come back with the result so a caller can decide
        # whether the edit moved a budget without retrieving the page again —
        # this route is admin-only and never capped, but an admin's correction
        # still has to be able to trip the threshold alert.
        rel = page["properties"].get("Project", {}).get("relation") or []
        d = page["properties"].get("Date", {}).get("date") or {}
        where = {"project_id": rel[0]["id"] if rel else None,
                 "date": (d.get("start") or "")[:10] or None}
        if not hours:  # 0, None -> remove, like a blanked cell on the weekly grid
            _notion.pages.update(entry_id, archived=True)
            return {"ok": True, "hours": 0, "deleted": True, **where}
        _notion.pages.update(entry_id, properties={"Hours": {"number": hours}})
        return {"ok": True, "hours": hours, "deleted": False, **where}


def _bare(notion_id: str | None) -> str:
    """Notion ids compare equal with or without dashes (env vars carry either)."""
    return (notion_id or "").replace("-", "").lower()


def _norm(name: str) -> str:
    """A name flattened for comparison: case and punctuation dropped.

    Enough to see that our project "FShip" is the ticket board's "Fship", and
    deliberately no cleverer than that — see match_project_option.
    """
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def set_cell(person_id: str, project_id: str, date: str, hours: float,
             enforce: bool = False, note: bool = False,
             description: str = "", task_url: str = "", task_label: str = "") -> dict:
    """Upsert the (person, project, date) cell to `hours`. 0/None deletes the entry.

    Filters on Person in the query (not a Python scan), paginates, and
    consolidates duplicates: the grid shows one summed cell, so a save must
    leave exactly one row behind (or none for 0).

    `enforce` opts the write into the project's budget cap. Note this is an
    upsert, not an append: typing 3 into a cell that held 5 *lowers* the month
    by 2, so the budget check compares against the **delta**, not the submitted
    hours. Getting that wrong would refuse every ordinary grid correction on a
    busy project.

    `note` opts it into "say what this was for" — but **only where this call
    actually creates an entry**. Correcting a number in a cell that already
    holds one is not a new piece of work to describe, and an entry logged
    before the rule existed must stay editable; demanding a description to fix
    a typo would make old rows unfixable, which is the same trap the budget cap
    avoids by never refusing a write that lowers a total. Deleting (`0`) asks
    for nothing either.
    """
    if enforce:
        # Warm the budget cache *before* taking the lock. On a cache miss this
        # pages every project, and _write_lock is global — holding it through
        # that cold read would stall every other save in the app behind it.
        budget_for(project_id)
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
        # "Say what you worked on" comes first, as it does in create_entry: when
        # a new cell is both undescribed and over budget, the description is the
        # answer the person can act on, and being told about the budget instead
        # only to be told about the description afterwards is two refusals for
        # one save. Only a *new* entry is asked (see the docstring).
        if note and hours and not keep:
            require_note(description, task_url)
        if enforce:
            # Duplicates get folded into one below, so what this cell currently
            # contributes to the month is their sum, not just the first row's.
            was = sum(m["properties"]["Hours"]["number"] or 0 for m in matches)
            check_budget(project_id, date, (hours or 0) - was)
        for extra in matches[1:]:  # duplicates from old races/forms: fold into one
            _notion.pages.update(extra["id"], archived=True)

        if not hours:  # 0, None -> remove
            if keep:
                _notion.pages.update(keep["id"], archived=True)
            return {"ok": True, "hours": 0}

        if keep:
            # an existing cell: only the number moves, so nothing new is being
            # described and the row keeps whatever description it already has
            _notion.pages.update(keep["id"], properties={
                "Hours": {"number": hours},
                "Person": {"people": [{"id": person_id}]},
            })
        else:
            # a brand-new entry — already checked above, before the budget and
            # before anything was written, so a refusal leaves nothing behind
            create_entry(person_id, project_id, date, hours,
                         description=description, task_url=task_url,
                         task_label=task_label)
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

# ...and what it might call the single-select naming the client/project. Matched
# on the whole normalised name, not a substring: "Project Phase" and "Project
# name" are not the column we mean.
_PROJECT_NAMES = ("project", "proyecto", "cliente", "client")

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
    """Which property on a ticket board is its title, its assignee, and — for
    creating a ticket — which single-select names the project.

    Ticket boards belong to other teams and get renamed freely, so nothing is
    addressed by a hardcoded name here — the lesson from alloc_person_prop.
    """
    now = time.time()
    hit = _task_schema_cache.get(ds_id)
    if hit and now - hit[0] < _TASK_SRC_TTL:
        return hit[1]
    info = {"title": None, "people": None, "email": None,
            "project": None, "project_options": []}
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
    for name, p in props.items():
        if p.get("type") == "select" and _norm(name) in _PROJECT_NAMES:
            info["project"] = name
            info["project_options"] = [o["name"] for o in p["select"].get("options") or []]
            break
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


# ---- creating a ticket -------------------------------------------------
#
# The third and strictest way in. Pasting a link needs nothing from Notion and
# searching needs *read* access to the ticket boards; creating needs *write*
# access to one named board, so it stays behind its own env var and simply
# doesn't appear until that is set.
#
# The board is named by config and never by the caller — the same rule that
# makes set_entry_hours and get_invoice refuse a page whose parent isn't ours.
# Writing is a side effect on another team's board, so it gets the narrowest
# possible target.

TICKET_BOARD = os.environ.get("TICKET_CREATE_DS_ID", "").strip()


def ticket_create_enabled() -> bool:
    return bool(TICKET_BOARD)


def match_project_option(project_name: str, options: list[str]) -> str | None:
    """Our project name -> the ticket board's own select option, or None.

    Matching stops at case and punctuation (`FShip` -> `Fship`). It is
    deliberately not fuzzier: our "Saltworks" and the board's "Salworks" are one
    keystroke apart, and so are plenty of client names — guessing files a real
    ticket against the wrong client. Anything unmatched is left for the person
    to pick from the board's real options instead.
    """
    want = _norm(project_name)
    if not want:
        return None
    for opt in options:
        if _norm(opt) == want:
            return opt
    return None


def ticket_board_info(project_name: str = "") -> dict:
    """What the "new ticket" dialog needs: the board's project options, and
    which one our project maps onto (empty when it maps onto none)."""
    if not TICKET_BOARD:
        return {"enabled": False, "options": [], "selected": "", "field": ""}
    schema = _task_schema(TICKET_BOARD)
    options = schema.get("project_options") or []
    return {
        "enabled": True,
        "field": schema.get("project") or "",
        "options": options,
        "selected": match_project_option(project_name, options) or "",
    }


def create_ticket(title: str, description: str = "", project_option: str = "",
                  person_id: str = "") -> dict:
    """Create a ticket on the configured board and return {id, url, title}.

    Only ever writes what the board actually has, resolved from its schema
    rather than by name (the alloc_person_prop lesson): the title property, the
    assignee, and the project select. Status, type and priority are left alone
    so the board's own defaults apply.

    `project_option` must be an option the board already has. An unknown name is
    dropped rather than sent, because Notion *creates* a select option for any
    name it doesn't recognise — a typo here would litter another team's board.

    The description becomes the page body: this board has no description
    property, and a Notion ticket's description is its page content anyway.
    """
    if not TICKET_BOARD:
        raise RuntimeError("No ticket board configured (TICKET_CREATE_DS_ID)")
    title = (title or "").strip()
    if not title:
        raise ValueError("A ticket needs a title")

    schema = _task_schema(TICKET_BOARD)
    title_prop = schema.get("title")
    if not title_prop:
        raise RuntimeError("That ticket board has no title property")

    props: dict = {title_prop: {"title": [{"text": {"content": title[:200]}}]}}
    if person_id and schema.get("people"):
        # assigning it to whoever logged it is what puts the new ticket in their
        # own my_tasks list, which filters on this very property
        props[schema["people"]] = {"people": [{"object": "user", "id": person_id}]}
    if project_option and schema.get("project"):
        if project_option in (schema.get("project_options") or []):
            props[schema["project"]] = {"select": {"name": project_option}}
        else:
            logging.warning("Ignoring unknown project option %r for the ticket board",
                            project_option)

    kwargs: dict = {"parent": {"type": "data_source_id", "data_source_id": TICKET_BOARD},
                    "properties": props}
    # Notion caps one rich-text object at 2000 characters, so a long
    # description becomes several paragraphs rather than a rejected write.
    description = (description or "").strip()[:8000]
    if description:
        kwargs["children"] = [
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": description[i:i + 1900]}}]}}
            for i in range(0, len(description), 1900)
        ]
    with _write_lock:
        page = _notion.pages.create(**kwargs)
    return {"id": page["id"], "title": title,
            "url": page.get("url") or f"https://www.notion.so/{_bare(page['id'])}"}


# ---- billing details (rate + who the bill is addressed to) --------------
#
# An invoice PDF needs three things this app never had: an hourly rate, a
# currency, and a client to address. All three belong to the *project*, and all
# three are curated in Notion rather than edited here — the same call made for
# the People roster, the Partner column and the PM/AM roles. The columns are
# added on boot the way the budget ones are, so the day this ships every
# project has somewhere to put them.
#
# A project with no rate is not an error: invoice_pdf.build simply drops the
# money columns and the document reads as a statement of hours. Nobody is going
# to fill a rate in for 35 projects on deploy day.

RATE_PROP = "Rate"
CURRENCY_PROP = "Currency"
CLIENT_NAME_PROP = "Client name"
CLIENT_EMAIL_PROP = "Client email"
CLIENT_ADDRESS_PROP = "Client address"

# Seeded onto a freshly created Currency column so the dropdown isn't empty.
# Notion invents an option for any name it is handed, so we only ever *read*
# this column — a currency we don't know still comes through fine.
_CURRENCY_OPTIONS = ("USD", "EUR", "GBP", "ARS")


def default_currency() -> str:
    return (os.environ.get("INVOICE_CURRENCY") or "USD").strip().upper()[:8] or "USD"


def ensure_billing_properties() -> None:
    """Add the rate / client columns to the Projects db if they're missing.

    Same shape as ensure_budget_properties: read the schema, add only what
    isn't there, one update. Safe to run on every boot.
    """
    ds = _notion.data_sources.retrieve(PROJECTS_DS)
    have = ds["properties"]
    missing = {}
    if RATE_PROP not in have:
        missing[RATE_PROP] = {"number": {}}
    if CURRENCY_PROP not in have:
        missing[CURRENCY_PROP] = {"select": {"options": [
            {"name": c} for c in _CURRENCY_OPTIONS]}}
    if CLIENT_NAME_PROP not in have:
        missing[CLIENT_NAME_PROP] = {"rich_text": {}}
    if CLIENT_EMAIL_PROP not in have:
        missing[CLIENT_EMAIL_PROP] = {"email": {}}
    if CLIENT_ADDRESS_PROP not in have:
        missing[CLIENT_ADDRESS_PROP] = {"rich_text": {}}
    if missing:
        _notion.data_sources.update(PROJECTS_DS, properties=missing)


def _plain(props: dict, name: str) -> str:
    text = props.get(name, {}).get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in text).strip()


def _billing_from_props(props: dict) -> dict:
    """The rate and client contact off a project page.

    Every read goes through .get() for the reason _budget_from_props does: these
    columns are addressed by name, and a rename in the Notion UI has taken two
    pages down before (see alloc_person_prop). A renamed column here must read
    as "no rate set" — the invoice loses its money columns — never as a 500.
    """
    rate = props.get(RATE_PROP, {}).get("number")
    sel = props.get(CURRENCY_PROP, {}).get("select") or {}
    return {
        "rate": float(rate) if rate is not None else 0.0,
        "currency": (sel.get("name") or default_currency()).upper(),
        "client_name": _plain(props, CLIENT_NAME_PROP),
        "client_email": (props.get(CLIENT_EMAIL_PROP, {}).get("email") or "").strip(),
        "client_address": _plain(props, CLIENT_ADDRESS_PROP),
    }


def project_billing(project_id: str) -> dict:
    """One project's billing block, read straight from its page.

    A page retrieve rather than a scan of list_projects: this runs when an
    invoice is opened or sent, where the caller wants *this* project's rate and
    not a list of 35.
    """
    blank = {"rate": 0.0, "currency": default_currency(), "client_name": "",
             "client_email": "", "client_address": ""}
    if not project_id:
        return blank
    try:
        page = _notion.pages.retrieve(project_id)
    except Exception:
        logging.warning("Could not read billing details for project %s", project_id)
        return blank
    return _billing_from_props(page.get("properties") or {})


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
    """Make sure the Invoices db can hold everything an invoice now records.

    Three generations of the same database: totals first, then the per-entry
    adjustments the detail view needs, then the number / rate / amount and the
    send log the PDF needs. Each was added on startup the way
    ensure_person_property does, so an existing Notion db catches up on boot
    rather than needing the setup script re-run.
    """
    if not INVOICES_DS:
        return
    ds = _notion.data_sources.retrieve(INVOICES_DS)
    have = ds["properties"]
    missing = {}
    if _ADJ_PROP not in have:
        missing[_ADJ_PROP] = {"rich_text": {}}
    if NUMBER_PROP not in have:
        missing[NUMBER_PROP] = {"rich_text": {}}
    if INV_RATE_PROP not in have:
        missing[INV_RATE_PROP] = {"number": {}}
    if AMOUNT_PROP not in have:
        missing[AMOUNT_PROP] = {"number": {}}
    if INV_CURRENCY_PROP not in have:
        missing[INV_CURRENCY_PROP] = {"rich_text": {}}
    if SENT_TO_PROP not in have:
        missing[SENT_TO_PROP] = {"rich_text": {}}
    if SENT_AT_PROP not in have:
        missing[SENT_AT_PROP] = {"date": {}}
    if CLIENT_NOTE_PROP not in have:
        missing[CLIENT_NOTE_PROP] = {"rich_text": {}}
    if missing:
        _notion.data_sources.update(INVOICES_DS, properties=missing)


# What the invoice *document* carries, on top of the hours the record already
# held. The rate and currency are copied off the project at save time rather
# than looked up when the PDF is drawn: a rate that changes in Notion next
# quarter must not silently restate a bill that has already gone out.
NUMBER_PROP = "Number"
INV_RATE_PROP = "Rate"
AMOUNT_PROP = "Amount"
INV_CURRENCY_PROP = "Currency"
SENT_TO_PROP = "Sent to"
SENT_AT_PROP = "Sent at"
CLIENT_NOTE_PROP = "Client note"   # printed on the PDF, unlike `Note`, which is internal


_NUMBER_RE = re.compile(r"(\d{4})-(\d+)\s*$")


def next_invoice_number(year: int) -> str:
    """The next number in this year's sequence, e.g. "2026-014".

    Derived from the numbers already filed rather than from a counter, because
    a counter would be a second source of truth living outside Notion — and
    someone will always renumber a row by hand. An optional
    INVOICE_NUMBER_PREFIX rides in front ("ZN-2026-014").

    Called under the write lock in save_invoice, so two invoices saved at once
    can't be handed the same number — within one process. That is the whole of
    it today (one free Render instance, one worker); on more than one worker two
    saves in the same year could still scan the same highest number, and this
    would need a uniqueness check after the write.
    """
    prefix = os.environ.get("INVOICE_NUMBER_PREFIX", "").strip()
    highest = 0
    for page in _query_all({"data_source_id": INVOICES_DS, "page_size": 100}):
        raw = _plain(page.get("properties") or {}, NUMBER_PROP)
        m = _NUMBER_RE.search(raw)
        if m and int(m.group(1)) == year:
            highest = max(highest, int(m.group(2)))
    return f"{prefix}{year}-{highest + 1:03d}"


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
        # the document's own fields — every one optional, since invoices filed
        # before the PDF existed have none of them
        "number": _plain(props, NUMBER_PROP),
        # None, not 0: an invoice deliberately billed as hours only stores a
        # real 0, and the export screen has to tell that apart from an invoice
        # filed before rates existed — otherwise re-saving the first one picks
        # up the project's current rate and quietly prices a free month.
        "rate": props.get(INV_RATE_PROP, {}).get("number"),
        "amount": props.get(AMOUNT_PROP, {}).get("number") or 0,
        "currency": _plain(props, INV_CURRENCY_PROP) or default_currency(),
        "client_note": _plain(props, CLIENT_NOTE_PROP),
        "sent_to": _plain(props, SENT_TO_PROP),
        "sent_at": ((props.get(SENT_AT_PROP, {}).get("date") or {}).get("start") or "")[:19],
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
                 adjustments: dict | None = None, rate: float = 0.0,
                 currency: str = "", client_note: str = "") -> dict:
    """File (or correct) the invoice for one project and one month.

    Upserts on (project, month), the same discipline as set_cell: saving July
    for a project twice is nearly always a correction, not a second bill. On a
    correction `Issued` keeps whatever it already said unless a new date is
    passed — and so does `Number`: a bill that has gone out keeps the number it
    went out with. Notion's own page history is the audit trail.

    `rate`/`currency` are copied onto the row rather than read from the project
    when the PDF is drawn, so next quarter's rate can't restate a bill already
    sent. `Amount` is the pre-tax subtotal — tax is a property of the sender
    (INVOICE_TAX_PCT), applied when the document is built.
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
        CLIENT_NOTE_PROP: {"rich_text": [{"text": {"content": (client_note or "")[:1900]}}]},
        INV_RATE_PROP: {"number": round(float(rate or 0), 2)},
        AMOUNT_PROP: {"number": round(float(hours_billed) * float(rate or 0), 2)},
        INV_CURRENCY_PROP: {"rich_text": [
            {"text": {"content": (currency or default_currency()).upper()[:8]}}]},
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
        # a number is assigned once and then kept: correcting July's hours must
        # not hand the client a second invoice number for the same bill
        if not (keep and _plain(keep.get("properties") or {}, NUMBER_PROP)):
            props[NUMBER_PROP] = {"rich_text": [{"text": {"content": next_invoice_number(
                dt.date.fromisoformat(month).year)}}]}
        if keep:
            _notion.pages.update(keep["id"], properties=props)
            page = _notion.pages.retrieve(keep["id"])
        else:
            page = _notion.pages.create(
                parent={"type": "data_source_id", "data_source_id": INVOICES_DS},
                properties=props)
    return dict(_invoice_row(page, pname, _person_name_map()), replaced=bool(keep))


def mark_invoice_sent(invoice_id: str, recipients: list[str]) -> dict:
    """Record that this invoice went out, and to whom.

    Written only after Gmail has accepted the message, so the row can't claim a
    send that failed. The parent check is get_invoice's, for the same reason:
    the id comes from a browser.
    """
    invoice = get_invoice(invoice_id)
    if not invoice:
        raise ValueError("that isn't an invoice")
    to = ", ".join(recipients)[:1900]
    _notion.pages.update(invoice_id, properties={
        SENT_TO_PROP: {"rich_text": [{"text": {"content": to}}]},
        SENT_AT_PROP: {"date": {"start": dt.datetime.now().astimezone().isoformat(
            timespec="seconds")}},
    })
    return dict(invoice, sent_to=to)


def entry_task(props: dict) -> dict:
    """The ticket on a Time Entries row, read tolerantly: both properties are
    optional (every entry logged before this feature has none) and a rename in
    the Notion UI must not take a report down."""
    url = props.get(_TASK_URL_PROP, {}).get("url") or ""
    text = props.get(_TASK_PROP, {}).get("rich_text") or []
    label = "".join(t.get("plain_text", "") for t in text).strip()
    return {"task_url": url, "task": label or ("Notion ticket" if url else "")}


# ---- absences ----------------------------------------------------------
#
# One row per absence, holding the whole stretch of days in a single Notion
# date property (start + end) — not one row per day the way allocations work.
# An absence is one decision with one reason, and a fortnight off shouldn't
# leave ten rows to delete. Days are expanded here whenever something needs to
# count them.
ABSENCES_DS = _ids.get("absences_ds_id")   # optional: unset until set up

# Notion's date filters compare against a range's *start*, so "ends on or after
# X" can't be asked for server-side. The query instead reaches back a bounded
# window before the period and the overlap is settled in Python — nobody books
# an absence longer than this, and the bound keeps the read small.
_MAX_ABSENCE_DAYS = 366
MAX_ABSENCE_REASON = 400


def absences_enabled() -> bool:
    return bool(ABSENCES_DS)


def weekdays_between(start: dt.date, end: dt.date) -> list[dt.date]:
    """Mon–Fri days in [start, end] — the days an absence actually costs.

    Weekends are dropped for the same reason the planner skips them: a Friday
    to Monday absence is two days off, not four.
    """
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def _absence_row(page: dict, people: dict) -> dict:
    props = page["properties"]
    date = props.get("Dates", {}).get("date") or {}
    start = (date.get("start") or "")[:10]
    end = (date.get("end") or start or "")[:10]
    who = props.get("Person", {}).get("people") or []
    pid = who[0]["id"] if who else None
    reason = props.get("Reason", {}).get("rich_text") or []
    return {
        "id": page["id"],
        "person_id": pid,
        # people properties come back nameless — resolve against the roster
        "person": people.get(pid, "(unknown)") if pid else "(unassigned)",
        "start": start,
        "end": end,
        "days": props.get("Days", {}).get("number") or 0,
        "reason": "".join(t.get("plain_text", "") for t in reason),
        "url": page.get("url", ""),
    }


def list_absences(date_from: str, date_to: str, person_id: str | None = None) -> list[dict]:
    """Every absence overlapping [date_from, date_to], earliest first.

    Overlapping, not contained: a fortnight off that starts in June is still
    what someone is doing on the 1st of July, and a dashboard that hid it would
    be lying about the week.
    """
    if not ABSENCES_DS:
        return []
    reach = (dt.date.fromisoformat(date_from) - dt.timedelta(days=_MAX_ABSENCE_DAYS)).isoformat()
    kwargs = {"data_source_id": ABSENCES_DS, "page_size": 100, "filter": {"and": [
        {"property": "Dates", "date": {"on_or_after": reach}},
        {"property": "Dates", "date": {"on_or_before": date_to}},
    ]}, "sorts": [{"property": "Dates", "direction": "ascending"}]}
    people = _person_name_map()
    out = []
    for page in _query_all(kwargs):
        row = _absence_row(page, people)
        if not row["start"] or row["end"] < date_from:   # ended before the period
            continue
        if person_id and row["person_id"] != person_id:
            continue
        out.append(row)
    return out


def add_absence(person_id: str | None, person_name: str, start: str, end: str,
                reason: str = "") -> dict:
    """File one absence. `end` may equal `start` for a single day."""
    if not ABSENCES_DS:
        raise ValueError("The Absences database isn't set up yet.")
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if last < first:
        raise ValueError("The end date is before the start date.")
    if (last - first).days > _MAX_ABSENCE_DAYS:
        raise ValueError("That range is longer than a year — log it in shorter stretches.")
    days = len(weekdays_between(first, last))
    label = first.strftime("%d %b") if first == last else f"{first:%d %b} – {last:%d %b %Y}"
    props = {
        "Absence": {"title": [{"text": {"content": f"{person_name or 'Absence'} · {label}"}}]},
        # a single-day absence still gets an explicit end, so every row reads
        # the same way in Notion and nothing downstream has to guess
        "Dates": {"date": {"start": start, "end": end if end != start else None}},
        "Days": {"number": days},
        "Reason": {"rich_text": [{"text": {"content": reason[:MAX_ABSENCE_REASON]}}]},
    }
    if person_id:
        props["Person"] = {"people": [{"id": person_id}]}
    with _write_lock:
        page = _notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": ABSENCES_DS},
            properties=props)
    return _absence_row(page, _person_name_map())


def delete_absence(absence_id: str, requester_id: str | None = None,
                   any_person: bool = False) -> dict:
    """Archive one absence by page id, refusing anything that isn't one.

    The id arrives from the browser, so two things are checked *before* the
    write, inside the same lock: that the page really is a row of this database
    (the way set_entry_hours checks a time entry's parent), and that the caller
    owns it — `any_person` is the admin's escape hatch. Otherwise anyone who
    could read an id could cancel somebody else's holiday.
    """
    if not ABSENCES_DS:
        raise ValueError("The Absences database isn't set up yet.")
    with _write_lock:
        page = _notion.pages.retrieve(absence_id)
        parent = page.get("parent") or {}
        if _bare(parent.get("data_source_id")) != _bare(ABSENCES_DS):
            raise ValueError("not an absence")
        row = _absence_row(page, _person_name_map())
        if not any_person and (not requester_id
                               or _bare(row["person_id"]) != _bare(requester_id)):
            raise PermissionError("that absence belongs to someone else")
        _notion.pages.update(absence_id, archived=True)
    return row


# ---- project budgets ---------------------------------------------------
#
# A project may carry an optional **monthly** hour budget: an allowance that
# resets on the 1st of every calendar month, never carrying over in either
# direction. Modelled on Harvest's `budget_is_monthly`, with the one thing
# Harvest doesn't have bolted on — a policy that can actually refuse a write.
#
# The config lives on the Projects data source rather than in a database of its
# own, because it's per-project *settings* (exactly one value per project), not
# dated events the way invoices and absences are. That also means it rides
# along on the list_projects query the app already runs, so a budget check on a
# hot write path costs no extra read.
#
# Two properties express all three behaviours that were asked for:
#
#     no limit, just track it   -> policy "Warn only"
#     hard stop at the budget   -> policy "Block over limit", Overrun % blank
#     allow up to 10% over      -> policy "Block over limit", Overrun % = 10
#     no budget at all          -> Monthly budget empty
#
# Empty is emphatically not 0. An empty budget means "not budgeted"; a budget of
# 0 means "no hours allowed here at all", and both are useful. (Harvest has the
# same trap and documents it: a blank per-person budget is ignored, a 0 is
# instantly over budget.)

BUDGET_PROP = "Monthly budget"
BUDGET_POLICY_PROP = "Budget policy"
BUDGET_OVERRUN_PROP = "Overrun %"
BUDGET_WARN_PROP = "Warn at %"
BUDGET_NOTIFIED_PROP = "Budget notified"

POLICY_WARN = "Warn only"
POLICY_BLOCK = "Block over limit"
BUDGET_POLICIES = (POLICY_WARN, POLICY_BLOCK)

# Percentage of the budget at which the warning fires. 95 rather than Harvest's
# 80: a monthly allowance is small enough that 80% is still an ordinary Tuesday.
_DEFAULT_WARN_PCT = 95.0


def default_warn_pct() -> float:
    try:
        return float(os.getenv("BUDGET_WARN_PCT", _DEFAULT_WARN_PCT))
    except ValueError:
        return _DEFAULT_WARN_PCT


def ensure_budget_properties() -> None:
    """Add the budget properties to the Projects db if they're missing.

    Same shape as ensure_person_property/ensure_task_properties: read the
    schema, add only what isn't there, one update. Safe to run on every boot.
    """
    ds = _notion.data_sources.retrieve(PROJECTS_DS)
    have = ds["properties"]
    missing = {}
    if BUDGET_PROP not in have:
        missing[BUDGET_PROP] = {"number": {}}
    if BUDGET_POLICY_PROP not in have:
        missing[BUDGET_POLICY_PROP] = {"select": {"options": [
            {"name": POLICY_WARN, "color": "yellow"},
            {"name": POLICY_BLOCK, "color": "red"},
        ]}}
    if BUDGET_OVERRUN_PROP not in have:
        missing[BUDGET_OVERRUN_PROP] = {"number": {}}
    if BUDGET_WARN_PROP not in have:
        missing[BUDGET_WARN_PROP] = {"number": {}}
    if BUDGET_NOTIFIED_PROP not in have:
        missing[BUDGET_NOTIFIED_PROP] = {"rich_text": {}}
    if missing:
        _notion.data_sources.update(PROJECTS_DS, properties=missing)


def _budget_from_props(props: dict) -> dict | None:
    """Parse the budget properties off a project page, or None if unbudgeted.

    Every read goes through .get(): these columns are addressed by name, and
    this app has already been bitten by someone renaming a Notion column (see
    alloc_person_prop, and the Time Entries 'Logged by' column that is
    currently called 'melisa'). A renamed budget column must read as "no
    budget" — the project simply stops being enforced — never as a 500.
    """
    hours = props.get(BUDGET_PROP, {}).get("number")
    if hours is None:
        return None
    sel = props.get(BUDGET_POLICY_PROP, {}).get("select") or {}
    policy = sel.get("name") or POLICY_WARN
    if policy not in BUDGET_POLICIES:
        policy = POLICY_WARN
    overrun = props.get(BUDGET_OVERRUN_PROP, {}).get("number") or 0
    warn = props.get(BUDGET_WARN_PROP, {}).get("number")
    notified = props.get(BUDGET_NOTIFIED_PROP, {}).get("rich_text") or []
    return {
        "hours": float(hours),
        "policy": policy,
        "overrun_pct": float(overrun),
        # None means "use the env default", resolved here so callers never have
        # to know the difference.
        "warn_pct": float(warn) if warn is not None else default_warn_pct(),
        "warn_pct_set": warn is not None,
        "notified": notified[0]["plain_text"] if notified else "",
        "limit": float(hours) * (1 + float(overrun) / 100),
    }


_BUDGET_TTL = 60.0
_budget_cache: dict = {"at": 0.0, "by_id": None}
_budget_lock = threading.Lock()


def project_budgets(refresh: bool = False) -> dict:
    """Cached {project_id: budget dict} for every project that has a budget.

    Cached for the same reason access_ids is: this is consulted on every hours
    write, and most of those writes are for projects with no budget at all —
    which this dict answers without touching Notion. A budget edited in Notion
    takes up to _BUDGET_TTL seconds to bite.
    """
    now = time.monotonic()
    if not refresh:
        with _budget_lock:
            if _budget_cache["by_id"] is not None and now - _budget_cache["at"] < _BUDGET_TTL:
                return _budget_cache["by_id"]
    try:
        by_id = {p["id"]: p["budget"] for p in list_projects(active_only=False)
                 if p.get("budget")}
    except Exception:
        logging.exception(
            "Reading project budgets failed — were the budget columns renamed in "
            "Notion? Treating every project as unbudgeted for now."
        )
        by_id = {}
    with _budget_lock:
        _budget_cache.update(at=now, by_id=by_id)
    return by_id


def budget_for(project_id: str) -> dict | None:
    return project_budgets().get(project_id)


def month_bounds(date_iso: str) -> tuple[str, str]:
    """The calendar month containing `date_iso`, as (first_day, last_day).

    Calendar month, decided: the 1st to the last day, the same window
    _period_range uses everywhere else in the app. No billing-cycle offsets and
    no proration for a project that starts mid-month.
    """
    d = dt.date.fromisoformat(date_iso[:10])
    first = d.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return first.isoformat(), (nxt - dt.timedelta(days=1)).isoformat()


def project_month_hours(project_id: str, date_iso: str) -> float:
    """Hours already tracked against one project in the calendar month of `date_iso`.

    Keyed on the entries' own Date, so a backfill logged today against an
    August date spends August's budget. Uses project_entries, which filters the
    Project relation inside the Notion query rather than paging the whole db.
    """
    first, last = month_bounds(date_iso)
    return sum(e["hours"] or 0 for e in project_entries(project_id, first, last))


def _num(x: float) -> str:
    """Format hours the way the app does elsewhere: 7 not 7.0, 7.5 stays 7.5."""
    return f"{x:g}"


class BudgetExceeded(Exception):
    """A write was refused because it would take a project past its cap.

    Carries the numbers so each route can phrase it in its own idiom rather
    than re-deriving them.
    """

    def __init__(self, project: str, month: str, budget: float, limit: float,
                 tracked: float, attempted: float):
        self.project = project
        self.month = month
        self.budget = budget
        self.limit = limit
        self.tracked = tracked
        self.attempted = attempted
        self.remaining = max(0.0, limit - tracked)
        over = " (including the allowed overrun)" if limit > budget else ""
        super().__init__(
            f"{project} has a {_num(budget)} h budget for {month} and "
            f"{_num(tracked)} h are already logged, so there "
            f"{'is' if self.remaining == 1 else 'are'} {_num(self.remaining)} h "
            f"left{over}. This entry would add {_num(attempted)} h."
        )


def check_budget(project_id: str, date: str, delta: float) -> None:
    """Raise BudgetExceeded if adding `delta` hours would cross the project's cap.

    Three ways this returns quietly, and each matters:

    * the project has no budget, or its policy is Warn only — the common case,
      answered from the cache without a Notion read;
    * `delta` is zero or negative. **A write that lowers a project's month
      total is never refused**, or a project sitting over its cap could never be
      corrected: every edit, including the one that fixes it, would be "over
      budget";
    * the projected total lands exactly on the limit. The cap refuses the hour
      that *crosses* it, so filling a 40 h budget to exactly 40 h is allowed.

    Note what isn't here: any notion of who is asking. Admins are exempt, but
    that's decided by the caller passing enforce=False — this module never
    reaches for auth, so the CLIs (which have no user at all) stay simple.
    """
    if delta <= 0:
        return
    b = budget_for(project_id)
    if not b or b["policy"] != POLICY_BLOCK:
        return
    tracked = project_month_hours(project_id, date)
    projected = tracked + delta
    if projected <= b["limit"] + 1e-9:
        return
    raise BudgetExceeded(
        project=_project_name_map().get(project_id, "This project"),
        month=dt.date.fromisoformat(date[:10]).strftime("%B %Y"),
        budget=b["hours"], limit=b["limit"], tracked=tracked, attempted=delta,
    )


_UNSET = object()  # "leave this property alone", which None can't mean here


def set_budget(project_id: str, hours: float | None = _UNSET,  # type: ignore[assignment]
               policy: str | None = None,
               overrun_pct: float | None = None,
               warn_pct: float | None = _UNSET) -> dict:  # type: ignore[assignment]
    """Write one project's budget settings. Only what's passed is written.

    `hours=None` **clears** the budget (the project stops being budgeted);
    omitting `hours` leaves it untouched. The two have to be distinguishable,
    or editing a policy on its own would silently wipe the number next to it.
    `warn_pct` works the same way: None clears it back to the env default,
    omitting it leaves whatever is there.

    Only the named properties are written — a project page's People property is
    the assignment list /schedule and /assignments both depend on, and
    rewriting the whole property bag would clobber it.

    The id arrives from the browser, so the page's parent is checked before the
    write, the way set_entry_hours and delete_absence do.
    """
    props: dict = {}
    if hours is not _UNSET:
        if hours is None:
            # Notion clears a number with an explicit null. This is the only
            # "off switch": an empty field, not a checkbox.
            props[BUDGET_PROP] = {"number": None}
        else:
            if hours < 0:
                raise ValueError("a budget can't be negative")
            props[BUDGET_PROP] = {"number": float(hours)}
    if policy is not None:
        if policy not in BUDGET_POLICIES:
            raise ValueError(f"unknown budget policy {policy!r}")
        props[BUDGET_POLICY_PROP] = {"select": {"name": policy}}
    if overrun_pct is not None:
        if overrun_pct < 0:
            raise ValueError("an overrun can't be negative")
        # 0 and blank mean the same thing (cap exactly at the budget), so store
        # the blank — otherwise the column fills up with noise-value zeroes.
        props[BUDGET_OVERRUN_PROP] = {"number": float(overrun_pct) or None}
    if warn_pct is not _UNSET:
        if warn_pct is None:
            # blanked on the page: fall back to BUDGET_WARN_PCT again
            props[BUDGET_WARN_PROP] = {"number": None}
        else:
            if not 0 < warn_pct <= 1000:
                raise ValueError("the warning threshold must be a percentage")
            props[BUDGET_WARN_PROP] = {"number": float(warn_pct)}
    if (policy is None and hours not in (_UNSET, None)
            and not budget_for(project_id)):
        # First budget on this project: default the policy rather than leaving
        # the select blank. Typing a number is meant to be one keystroke
        # sequence — nobody is choosing 37 policies up front — and Warn only is
        # the safe default, since it changes nothing about who can log time.
        props[BUDGET_POLICY_PROP] = {"select": {"name": POLICY_WARN}}
    if not props:
        return budget_for(project_id) or {}
    with _write_lock:
        page = _notion.pages.retrieve(project_id)
        parent = page.get("parent") or {}
        if _bare(parent.get("data_source_id")) != _bare(PROJECTS_DS):
            raise ValueError("not a project")
        _notion.pages.update(project_id, properties=props)
    project_budgets(refresh=True)  # the page re-renders straight after this
    return budget_for(project_id) or {}


def budget_alert(project_id: str, date: str) -> dict | None:
    """Return an alert payload if this project just crossed a threshold, else None.

    Called after a successful write. Two levels — `warn` at the project's own
    percentage and `over` at 100% — and each fires **once per project per
    month**, recorded in the Budget notified property as e.g. "2026-08:over".
    That stamp is Harvest's `over_budget_notification_date` idea: without it,
    every subsequent entry in an over-budget month sends another email.

    A new month means the stamp no longer matches, so it fires again — which is
    the intended behaviour for a budget that resets monthly.

    Deliberately indifferent to who logged the hours. Admins are never blocked,
    so an admin overrun is exactly the case this has to catch.
    """
    b = budget_for(project_id)
    if not b:
        return None
    tracked = project_month_hours(project_id, date)
    # A 0 h budget can't be divided by, but it must still alert: non-admins are
    # refused outright, so the only way hours reach a 0-budget project is an
    # admin going past the cap — which is exactly what this is here to catch.
    # Guard the division, not the whole function.
    pct = (100.0 if tracked > 0 else 0.0) if not b["hours"] else tracked / b["hours"] * 100
    if pct >= 100:
        level = "over"
    elif pct >= b["warn_pct"]:
        level = "warn"
    else:
        return None
    month = date[:7]
    stamp = f"{month}:{level}"
    # "over" supersedes "warn" within a month; dropping back never re-fires.
    if b["notified"] == stamp or (level == "warn" and b["notified"] == f"{month}:over"):
        return None
    with _write_lock:
        _notion.pages.update(project_id, properties={
            BUDGET_NOTIFIED_PROP: {"rich_text": [{"text": {"content": stamp}}]},
        })
    project_budgets(refresh=True)
    return {
        "level": level,
        "project": _project_name_map().get(project_id, "A project"),
        "project_id": project_id,
        "month": dt.date.fromisoformat(date[:10]).strftime("%B %Y"),
        "budget": b["hours"], "tracked": tracked, "pct": pct,
        "remaining": b["hours"] - tracked,
        "policy": b["policy"], "limit": b["limit"],
    }


# ---- project roles (PM / Account manager) -------------------------------
#
# "Who do I chase about this project" — a PM and an Account manager, curated
# in the same Projects data source the budget columns live on. Two people
# properties, each treated as holding **at most one** person: Notion has no
# single-person property type, so the app enforces that itself — readers take
# the first entry, writers replace the whole array. Both ride on the same
# list_projects read the budget already does, so neither costs an extra call.

PM_PROP = "PM"
AM_PROP = "Account manager"
ROLE_PROPS = {"pm": PM_PROP, "am": AM_PROP}


def ensure_role_properties() -> None:
    """Add the PM / Account manager properties to the Projects db if missing.

    Same shape as ensure_budget_properties/ensure_person_property: read the
    schema, add only what isn't there, one update. Safe to run on every boot.
    """
    ds = _notion.data_sources.retrieve(PROJECTS_DS)
    have = ds["properties"]
    missing = {}
    if PM_PROP not in have:
        missing[PM_PROP] = {"people": {}}
    if AM_PROP not in have:
        missing[AM_PROP] = {"people": {}}
    if missing:
        _notion.data_sources.update(PROJECTS_DS, properties=missing)


def _role_from_props(props: dict, role: str) -> str | None:
    """The Notion user id sitting in `role`'s people property, or None.

    Every read goes through .get(): this app has been taken down twice by a
    renamed Notion column (alloc_person_prop; Time Entries' "Logged by",
    currently called "melisa"). A renamed role column must read as "no PM" —
    the project just stops being filterable — never as a 500. A second person
    added by hand in Notion is ignored; only the first is ever read.
    """
    people = props.get(ROLE_PROPS[role], {}).get("people") or []
    return people[0]["id"] if people else None


def set_project_role(project_id: str, role: str, person_id: str | None) -> dict:
    """Set a project's PM or Account manager. person_id=None clears it.

    Rejects an unknown role and a person who isn't on the roster — both ids
    arrive from the browser, same posture as set_entry_hours refusing a page
    outside its data source. Deliberately doesn't touch the project's People
    property: a PM who never logs hours on the project is normal, and
    membership stays the separate, explicit thing it is today.
    """
    if role not in ROLE_PROPS:
        raise ValueError(f"unknown role {role!r}")
    if person_id is not None and person_id not in {p["id"] for p in list_people()}:
        raise ValueError("that person isn't on the roster")
    with _write_lock:
        page = _notion.pages.retrieve(project_id)
        parent = page.get("parent") or {}
        if _bare(parent.get("data_source_id")) != _bare(PROJECTS_DS):
            raise ValueError("not a project")
        _notion.pages.update(project_id, properties={
            ROLE_PROPS[role]: {"people": [{"id": person_id}] if person_id else []},
        })
    return {"ok": True, "role": role, "person_id": person_id}


# ---- partners (client umbrellas) ---------------------------------------
#
# Some clients are ours directly; others arrive under a partner agency — Bear
# brings Streamside and True Temper, Telus brings its own. A partner is *not* a
# database of its own: it is one `Partner` select column on Projects, curated
# in Notion beside Active and the budget columns, because a partner has no
# attributes beyond its name and the projects under it. No partner = direct.
#
# A partner shows up in two different ways, and the difference matters:
#
#   * as a **filter** — "Bear" means whatever is under Bear *today*. Every page
#     that filters projects takes a repeated ?partner=, expanded to project ids
#     at request time, so a saved link that names the umbrella stays right when
#     a project joins or leaves it. A frozen list of ?project= ids would not.
#   * as an **umbrella project** — one real Projects row per partner, ticked
#     `Umbrella`, that hours and allocations can be booked against when the work
#     is the partner's own rather than any one client's. It has to be a real
#     project: a Time Entry's (and an Allocation's) Project is a relation into
#     this data source, and a select value is not bookable.
#
# The umbrella row carries its own Partner, so it sits *inside* the Bear
# filter next to Bear's client projects — booking "Bear" and booking
# "Streamside" both roll up under Bear, which is the whole point.

PARTNER_PROP = "Partner"
UMBRELLA_PROP = "Umbrella"

# Seeded onto a freshly created Partner column so a new deploy has the two
# partners this workspace actually has. Only ever read when the column doesn't
# exist yet — after that Notion's own option list is the source of truth.
_SEED_PARTNERS = "Bear,Telus"

_PARTNER_TTL = 300.0
_partner_cache: dict = {"at": 0.0, "names": None}
_partner_lock = threading.Lock()


def ensure_partner_properties() -> None:
    """Add the Partner / Umbrella columns to the Projects db if they're missing.

    Same shape as ensure_budget_properties/ensure_role_properties: read the
    schema, add only what isn't there, one update. Safe to run on every boot.
    The select's options are seeded once, at creation — never afterwards, since
    from then on Notion's list is what the app offers and validates against.
    """
    ds = _notion.data_sources.retrieve(PROJECTS_DS)
    have = ds["properties"]
    missing = {}
    if PARTNER_PROP not in have:
        seed = [p.strip() for p in os.getenv("PARTNERS", _SEED_PARTNERS).split(",") if p.strip()]
        missing[PARTNER_PROP] = {"select": {"options": [{"name": p} for p in seed]}}
    if UMBRELLA_PROP not in have:
        missing[UMBRELLA_PROP] = {"checkbox": {}}
    if missing:
        _notion.data_sources.update(PROJECTS_DS, properties=missing)
        with _partner_lock:  # a newly seeded option list must not read as empty
            _partner_cache.update(at=0.0, names=None)


def _partner_from_props(props: dict) -> str:
    """The partner umbrella this project sits under, or "" for a direct client.

    Through .get() like every other property read here: this app has been taken
    down twice by a renamed Notion column (alloc_person_prop; Time Entries'
    "Logged by"). A renamed Partner column must read as "everyone is direct" —
    the filter simply stops offering partners — never as a 500.
    """
    sel = props.get(PARTNER_PROP, {}).get("select") or {}
    return sel.get("name") or ""


def _partner_options() -> list[str]:
    """Partner names as Notion has them, in the option order set there. Cached
    briefly: this is consulted on every page that renders the project filter."""
    now = time.monotonic()
    with _partner_lock:
        if _partner_cache["names"] is not None and now - _partner_cache["at"] < _PARTNER_TTL:
            return list(_partner_cache["names"])
    names: list[str] = []
    try:
        props = _notion.data_sources.retrieve(data_source_id=PROJECTS_DS)["properties"]
        prop = props.get(PARTNER_PROP) or {}
        if prop.get("type") == "select":
            names = [o["name"] for o in (prop.get("select") or {}).get("options", [])]
        elif PARTNER_PROP in props:
            logging.warning("Projects' %r column is a %s, not a select — partners are off "
                            "until it's a select again.", PARTNER_PROP, prop.get("type"))
    except Exception:
        logging.exception("Could not read the Projects schema for partner options.")
        return []   # not cached: a transient failure shouldn't hide partners for 5 min
    with _partner_lock:
        _partner_cache.update(at=now, names=list(names))
    return names


def list_partners(projects: list[dict] | None = None) -> list[str]:
    """The partners to offer, in Notion's option order.

    The schema leads, so an option added in Notion is offerable before any
    project uses it. Anything the given projects actually carry is appended, so
    deleting an option in Notion doesn't strand the rows still set to it.
    """
    names = _partner_options()
    seen = set(names)
    for p in projects or []:
        name = p.get("partner")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def set_project_partner(project_id: str, partner: str) -> dict:
    """Put a project under a partner umbrella, or "" to make it direct again.

    Refuses a name Notion doesn't already have as an option. That is not
    pedantry: Notion *creates* a select option for any name it's handed, so a
    typo here would quietly invent a third partner and file a client under it
    (the same trap create_ticket documents for the ticket board's project
    column). New partners are added in Notion, or by src/setup_partners.py.
    """
    partner = (partner or "").strip()
    if partner and partner not in _partner_options():
        raise ValueError(f"{partner!r} isn't a partner — add it in Notion first")
    with _write_lock:
        page = _notion.pages.retrieve(project_id)
        parent = page.get("parent") or {}
        if _bare(parent.get("data_source_id")) != _bare(PROJECTS_DS):
            raise ValueError("not a project")
        _notion.pages.update(project_id, properties={
            PARTNER_PROP: {"select": {"name": partner} if partner else None},
        })
    return {"ok": True, "project_id": project_id, "partner": partner}


def umbrella_project(partner: str, projects: list[dict] | None = None) -> dict | None:
    """The partner's own bookable project row, or None if it has none yet."""
    for p in (projects if projects is not None else list_projects(active_only=False)):
        if p.get("umbrella") and p.get("partner") == partner:
            return p
    return None


def create_umbrella_project(partner: str) -> dict:
    """Create the row a partner's own hours get booked against. Idempotent —
    returns the existing umbrella if there is one, so the setup script can be
    re-run to pick up a partner added later.

    Titled after the partner with nothing appended: this name ends up in
    reports, invoices and client-facing exports, where "Bear" reads and
    "Bear (umbrella)" doesn't. The Umbrella tick is what tells them apart, and
    it's what the UI badges.
    """
    partner = (partner or "").strip()
    if not partner:
        raise ValueError("a partner name is required")
    if partner not in _partner_options():
        raise ValueError(f"{partner!r} isn't a partner — add it in Notion first")
    existing = umbrella_project(partner)
    if existing:
        return existing
    page = _notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": PROJECTS_DS},
        properties={
            "Name": {"title": [{"type": "text", "text": {"content": partner}}]},
            "Active": {"checkbox": True},
            PARTNER_PROP: {"select": {"name": partner}},
            UMBRELLA_PROP: {"checkbox": True},
        },
    )
    return {"id": page["id"], "name": partner, "active": True,
            "partner": partner, "umbrella": True}


# ---- goals -------------------------------------------------------------
#
# A goal is a named bucket of work inside a project ("New homepage",
# "Maintenance") that logged hours are filed under. Its own database, related
# from Time Entries — see src/setup_goals_db.py for why not a select column.
#
# Two shapes, told apart by `Target basis`:
#
#   * Total     — 80 h for the homepage. Spent once, then Done. Its meter reads
#                 the goal's *lifetime* hours.
#   * Per month — 10 h of maintenance a month. One row that January's and
#                 December's entries both point at, never Done. Its meter reads
#                 the *month's* hours.
#
# Nothing here enforces a target. A cap on a goal would only teach people to
# log against no goal, which destroys the data the feature exists to collect.
GOALS_DS = _ids.get("goals_ds_id")   # optional: unset until set up

GOAL_STATUSES = ("Open", "Done", "Dropped")
GOAL_BASES = ("Total", "Per month")
MAX_GOAL_NAME = 120

# One assign request touches at most this many entries. The browser sends a
# long selection in batches of this size so it can show real progress — Notion
# has no bulk update, so filing 200 entries is 200 round trips at ~3/s.
MAX_GOAL_ASSIGN = 25

_GOAL_TTL = 60.0
_GOAL_PROP_TTL = 300.0        # a schema lookup, so it ages like _task_schema
_GOAL_PROP_DEFAULT = "Goal"   # the name we create; never the one we rely on
_goal_cache: dict = {"at": 0.0, "rows": None}
_goal_prop_cache: dict = {"at": 0.0, "name": None}
_goal_totals_cache: dict = {}    # project id -> (fetched_at, {goal id: hours})


def goals_enabled() -> bool:
    return bool(GOALS_DS)


def ensure_goal_property() -> None:
    """Make sure Time Entries has the Goal relation; add it if missing.

    Runs at startup like ensure_person_property. Does nothing at all until the
    Goals db exists, so the app boots fine before src/setup_goals_db.py is run.
    """
    if not GOALS_DS:
        return
    if goal_prop(refresh=True):
        return
    _notion.data_sources.update(TIME_DS, properties={
        _GOAL_PROP_DEFAULT: {"relation": {"data_source_id": GOALS_DS,
                                          "single_property": {}}},
    })
    goal_prop(refresh=True)


def goal_prop(refresh: bool = False) -> str | None:
    """The name of the Time Entries relation that points at Goals.

    Resolved from the schema by *target data source*, not by name: renaming a
    column in the Notion UI has taken this app down before (alloc_person_prop —
    the Allocations people column was renamed to `val`, and it 500'd two pages),
    and the Time Entries "Logged by" column is called `melisa` today. A relation
    knows what it points at, so the name is never load-bearing.

    None means "not set up" everywhere it's read, which degrades to a page with
    no goals rather than an error.
    """
    if not GOALS_DS:
        return None
    now = time.time()
    if not refresh and _goal_prop_cache["name"] and now - _goal_prop_cache["at"] < _GOAL_PROP_TTL:
        return _goal_prop_cache["name"]
    try:
        props = _notion.data_sources.retrieve(TIME_DS)["properties"]
    except Exception:
        logging.warning("Could not read the Time Entries schema to find the Goal relation")
        return _goal_prop_cache["name"]
    found = None
    for name, spec in props.items():
        if spec.get("type") != "relation":
            continue
        if _bare(spec.get("relation", {}).get("data_source_id")) == _bare(GOALS_DS):
            found = name
            if name == _GOAL_PROP_DEFAULT:
                break     # prefer the name we create, if several ever point here
    if found and found != _GOAL_PROP_DEFAULT:
        logging.warning("Time Entries' Goal relation is named %r — reading it anyway", found)
    _goal_prop_cache.update({"at": now, "name": found})
    return found


def _goal_row(page: dict, pname: dict) -> dict:
    props = page["properties"]
    title = props.get("Goal", {}).get("title") or []
    rel = props.get("Project", {}).get("relation") or []
    pid = rel[0]["id"] if rel else None
    basis = (props.get("Target basis", {}).get("select") or {}).get("name") or "Total"
    status = (props.get("Status", {}).get("select") or {}).get("name") or "Open"
    note = props.get("Note", {}).get("rich_text") or []
    return {
        "id": page["id"],
        "name": "".join(t.get("plain_text", "") for t in title).strip() or "(untitled)",
        "project_id": pid,
        "project": pname.get(pid, "(none)") if pid else "(none)",
        "target": props.get("Target hours", {}).get("number"),
        "basis": basis if basis in GOAL_BASES else "Total",
        "status": status if status in GOAL_STATUSES else "Open",
        "started": (props.get("Started", {}).get("date") or {}).get("start"),
        "due": (props.get("Due", {}).get("date") or {}).get("start"),
        "note": "".join(t.get("plain_text", "") for t in note).strip(),
    }


def all_goals(refresh: bool = False) -> list[dict]:
    """Every goal, cached ~60 s.

    One read for the lot: there are a handful per project, the picker and the
    name resolution both want them on every page load, and the alternative is a
    query per project. Every read goes through .get() — a renamed or missing
    column reads as a default, never a KeyError.
    """
    if not GOALS_DS:
        return []
    now = time.time()
    if not refresh and _goal_cache["rows"] is not None and now - _goal_cache["at"] < _GOAL_TTL:
        return _goal_cache["rows"]
    pname = _project_name_map()
    try:
        pages = _query_all({"data_source_id": GOALS_DS, "page_size": 100})
    except Exception:
        logging.warning("Goals query failed — the app carries on without goals")
        return _goal_cache["rows"] or []
    rows = [_goal_row(p, pname) for p in pages]
    rows.sort(key=lambda g: (g["status"] != "Open", g["name"].lower()))
    _goal_cache.update({"at": now, "rows": rows})
    return rows


def list_goals(project_id: str | None = None, open_only: bool = False) -> list[dict]:
    """Goals, optionally for one project and optionally only the open ones."""
    rows = all_goals()
    if project_id:
        rows = [g for g in rows if g["project_id"] == project_id]
    if open_only:
        rows = [g for g in rows if g["status"] == "Open"]
    return rows


def goal_map() -> dict:
    return {g["id"]: g for g in all_goals()}


def other_project_goal_names(project_id: str | None) -> list[dict]:
    """Goal names in use on *other* projects, most-used first.

    Offered when creating a goal because the cross-project report groups by
    name: "Maintenance" spelled two ways is two rows in a report that exists to
    put them in one. Names this project already has are left out — it has them.
    """
    mine = {_norm(g["name"]) for g in all_goals() if g["project_id"] == project_id}
    counts: dict = {}
    for g in all_goals():
        if g["project_id"] == project_id or g["status"] != "Open":
            continue
        key = _norm(g["name"])
        if not key or key in mine:
            continue
        row = counts.setdefault(key, {"name": g["name"], "projects": set()})
        row["projects"].add(g["project_id"])
    out = [{"name": r["name"], "count": len(r["projects"])} for r in counts.values()]
    out.sort(key=lambda r: (-r["count"], r["name"].lower()))
    return out


def entry_goal(props: dict) -> dict:
    """The goal on a Time Entries row, read tolerantly.

    Every entry logged before this feature has none, the column may not exist
    at all, and the name is resolved against the goal list rather than trusted
    from the relation (a relation payload carries an id, never a title).
    """
    prop = goal_prop()
    if not prop:
        return {"goal_id": None, "goal": ""}
    rel = props.get(prop, {}).get("relation") or []
    gid = rel[0]["id"] if rel else None
    if not gid:
        return {"goal_id": None, "goal": ""}
    g = goal_map().get(gid)
    return {"goal_id": gid, "goal": g["name"] if g else "(deleted goal)"}


def create_goal(name: str, project_id: str, target: float | None = None,
                basis: str = "Total", status: str = "Open",
                due: str | None = None) -> dict:
    """Create one goal against a project. Returns it in list_goals' shape."""
    if not GOALS_DS:
        raise ValueError("goals are not set up")
    name = (name or "").strip()[:MAX_GOAL_NAME]
    if not name:
        raise ValueError("a goal needs a name")
    if project_id not in _project_name_map():
        raise ValueError("unknown project")
    if basis not in GOAL_BASES:
        basis = "Total"
    if status not in GOAL_STATUSES:
        status = "Open"
    props = {
        "Goal": {"title": [{"text": {"content": name}}]},
        "Project": {"relation": [{"id": project_id}]},
        "Status": {"select": {"name": status}},
        "Target basis": {"select": {"name": basis}},
        "Started": {"date": {"start": dt.date.today().isoformat()}},
    }
    if target is not None:
        props["Target hours"] = {"number": float(target)}
    if due:
        # a standing goal leaves this empty — that is what "it never ends"
        # looks like in the data
        props["Due"] = {"date": {"start": due}}
    page = _notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": GOALS_DS}, properties=props)
    all_goals(refresh=True)
    return _goal_row(page, _project_name_map())


def _own_goal(goal_id: str) -> dict:
    """A goal by id, refusing anything that isn't one of ours.

    Goal ids arrive from the browser, so the parent is checked before any
    write — the same rule get_invoice and delete_absence follow.
    """
    page = _notion.pages.retrieve(goal_id)
    parent = page.get("parent") or {}
    if _bare(parent.get("data_source_id")) != _bare(GOALS_DS):
        raise ValueError("not a goal")
    return page


def update_goal(goal_id: str, name: str | None = None, target: float | None = _UNSET,
                basis: str | None = None, status: str | None = None,
                due: str | None = _UNSET) -> dict:
    """Edit one goal. Only the named fields are written.

    `target` and `due` distinguish "leave it alone" (_UNSET) from "clear it"
    (None) — set_budget makes the same distinction, and for the same reason:
    editing a status must not silently wipe the target beside it.
    """
    if not GOALS_DS:
        raise ValueError("goals are not set up")
    _own_goal(goal_id)
    props: dict = {}
    if name is not None:
        name = name.strip()[:MAX_GOAL_NAME]
        if not name:
            raise ValueError("a goal needs a name")
        props["Goal"] = {"title": [{"text": {"content": name}}]}
    if target is not _UNSET:
        props["Target hours"] = {"number": float(target) if target is not None else None}
    if basis is not None:
        if basis not in GOAL_BASES:
            raise ValueError("unknown target basis")
        props["Target basis"] = {"select": {"name": basis}}
    if status is not None:
        if status not in GOAL_STATUSES:
            raise ValueError("unknown status")
        props["Status"] = {"select": {"name": status}}
    if due is not _UNSET:
        props["Due"] = {"date": {"start": due} if due else None}
    if props:
        _notion.pages.update(goal_id, properties=props)
    all_goals(refresh=True)
    _goal_totals_cache.clear()
    return goal_map().get(goal_id) or {}


class GoalInUse(Exception):
    """A goal still has hours filed under it, so it can't be deleted.

    Carries the count so the refusal can say how many rather than just no —
    the difference between "unfile these 12 first" and a dead end.
    """

    def __init__(self, count: int, more: bool = False):
        self.count, self.more = count, more
        n = f"{count}+" if more else str(count)
        super().__init__(f"{n} {'entry is' if count == 1 and not more else 'entries are'} "
                         f"still filed under this goal")


def goal_entry_count(goal_id: str, cap: int = 500) -> tuple[int, bool]:
    """How many time entries are filed under a goal, over all time.

    Deliberately unbounded by date: a goal deleted while a January entry still
    points at it would leave that entry pointing at nothing, which reads as
    "(deleted goal)" in every report from then on.

    Counted rather than merely detected, so a refusal can name the number, and
    capped so a goal with thousands of entries doesn't page through them all to
    tell you what the first page already proves.

    **Fails closed.** Everywhere else an unresolvable Goal column degrades to
    "no goals" and a page renders without them; here the same silence would
    read as "nothing is filed under this goal, go ahead and delete it" — and
    the column has been unresolvable before (this relation is named `val` in
    Notion today; see goal_prop). A count that cannot be taken is not zero.
    """
    prop = goal_prop()
    if not prop:
        raise ValueError(
            "the Goal column on Time Entries can't be read right now, so there "
            "is no way to tell whether hours are filed under this goal")
    if not goal_id:
        return 0, False
    seen = 0
    kwargs = {"data_source_id": TIME_DS, "page_size": min(100, max(1, cap)),
              "filter": {"property": prop, "relation": {"contains": goal_id}}}
    while True:
        res = _notion.data_sources.query(**kwargs)
        seen += len(res["results"])
        if seen >= cap:
            return cap, res.get("has_more", False) or seen > cap
        if not res.get("has_more"):
            return seen, False
        kwargs["start_cursor"] = res["next_cursor"]


def delete_goal(goal_id: str) -> dict:
    """Delete a goal — but only once nothing is filed under it.

    The check is here rather than in the route, and it is not optional: a goal
    is only ever addressed by an id that came from a browser, and the cost of
    getting this wrong is silent — the hours survive, but they point at a page
    that no longer exists, so they vanish from every named row of the block and
    from Unassigned at the same time.

    Closing a goal (`Status: Done`) is the non-destructive alternative and the
    one to reach for when the work really happened: it keeps the hours and the
    history, and only takes the goal out of the picker.

    This *archives* the page rather than destroying it, so a delete that turns
    out to have been a mistake is recoverable from Notion's trash — which is
    also what keeps the narrow race with `set_entry_goals` (see there) from
    being worth a global lock across a whole batch.
    """
    if not GOALS_DS:
        raise ValueError("goals are not set up")
    _own_goal(goal_id)          # refuses any page that isn't one of our goals
    # The informative count runs *outside* the lock. Being told "no, 312 entries
    # are filed under this" is the common answer here — that path never writes,
    # so it has no business taking a global lock, and out here it can afford to
    # count past a single page and name a real number.
    count, more = goal_entry_count(goal_id)
    if count:
        raise GoalInUse(count, more)
    # Then re-check and archive under the lock together, the way delete_absence
    # does: without it an entry filed between the count and the write is
    # stranded by a delete that had already decided the goal was empty. One row
    # is all it takes to refuse, so this is a single one-row query — nothing in
    # here takes the lock again.
    with _write_lock:
        again, _ = goal_entry_count(goal_id, cap=1)
        if again:
            raise GoalInUse(again, True)
        _notion.pages.update(goal_id, archived=True)
    all_goals(refresh=True)     # a Notion read; not worth holding the lock for
    _goal_totals_cache.clear()
    return {"ok": True, "deleted": True}


def set_entry_goals(entry_ids: list[str], goal_id: str | None,
                    allowed_ids: set | None = None,
                    project_id: str | None = None) -> dict:
    """File a batch of existing entries under a goal (None clears it).

    Addressed by page id, like set_entry_hours — but `allowed_ids` is what makes
    that safe here without a retrieve per entry. The caller passes the ids it
    just read for the project and period on screen, and anything outside that
    set is refused: one query validates the whole batch, instead of doubling the
    round trips to check each page's parent, and it also pins an entry to the
    project whose goal it's being filed under.

    **No `_write_lock`.** It's global and non-reentrant, and a batch of 25
    updates would stall every other save in the app behind it for seconds.

    Exactly one other write reads the property this one writes: `delete_goal`
    counts entries filed under a goal before archiving it. So there is a real
    window here, left open deliberately — an admin deleting a goal between two
    batches of a long filing would leave the rest of the batch pointing at an
    archived page. It needs both admins acting on the same goal in the same
    moment; `delete_goal` refuses the instant anything is filed under it, and
    it *archives* rather than destroys, so Notion's trash can restore the page
    and the entries with it. Locking the whole batch to prevent that would cost
    every save in the app seconds of waiting, every time anyone files anything.

    Writing only the Goal property matters as much: rewriting a page's whole
    property bag would clobber whatever else is on the entry.
    """
    prop = goal_prop()
    if not prop:
        raise ValueError("goals are not set up")
    ids = [i for i in dict.fromkeys(entry_ids or []) if i]
    if not ids:
        return {"ok": True, "updated": 0, "failed": []}
    if len(ids) > MAX_GOAL_ASSIGN:
        # Refused whole rather than half-applied, the rule clear_week follows.
        raise ValueError(f"too many entries in one batch (max {MAX_GOAL_ASSIGN})")
    if allowed_ids is not None:
        bare = {_bare(i) for i in allowed_ids}
        if any(_bare(i) not in bare for i in ids):
            raise ValueError("an entry is not in this project and period")
    if goal_id:
        g = goal_map().get(goal_id)
        if not g:
            all_goals(refresh=True)
            g = goal_map().get(goal_id)
        if not g:
            raise ValueError("unknown goal")
        # The goal must belong to the same project as the entries. The UI never
        # offers another project's goal, but the id comes from the browser —
        # and a mis-filed entry would go missing from *both* sides: absent from
        # the named rows of either project's block (its goal isn't theirs) and
        # absent from Unassigned (it has one), while still counting toward the
        # project total the block is built never to contradict.
        if project_id and g["project_id"] != project_id:
            raise ValueError("that goal belongs to another project")
    value = {"relation": [{"id": goal_id}] if goal_id else []}
    updated, failed = 0, []
    for eid in ids:
        try:
            _notion.pages.update(eid, properties={prop: value})
            updated += 1
        except Exception as exc:      # one bad row must not lose the other 24
            logging.warning("Could not file entry %s under a goal: %s", eid, exc)
            failed.append(eid)
    _goal_totals_cache.clear()
    return {"ok": not failed, "updated": updated, "failed": failed}


def goal_totals(project_id: str) -> dict:
    """Lifetime hours per goal for one project — {goal id: hours}.

    A `Total` goal ("80 h for the homepage") is measured over its whole life,
    not the month on screen, so this reads the project's entries with no date
    bound. One query per project rather than one per goal, cached ~60 s, and
    only ever called on a page that shows a target.
    """
    if not GOALS_DS:
        return {}
    prop = goal_prop()
    if not prop:
        return {}
    now = time.time()
    hit = _goal_totals_cache.get(project_id)
    if hit and now - hit[0] < _GOAL_TTL:
        return hit[1]
    try:
        pages = _query_all({
            "data_source_id": TIME_DS, "page_size": 100,
            "filter": {"and": [
                {"property": "Project", "relation": {"contains": project_id}},
                {"property": prop, "relation": {"is_not_empty": True}},
            ]},
        })
    except Exception:
        logging.warning("Could not read lifetime goal hours for project %s", project_id)
        return hit[1] if hit else {}
    totals: dict = {}
    for page in pages:
        props = page["properties"]
        rel = props.get(prop, {}).get("relation") or []
        if not rel:
            continue
        totals[rel[0]["id"]] = totals.get(rel[0]["id"], 0) + (props["Hours"]["number"] or 0)
    totals = {k: round(v, 2) for k, v in totals.items()}
    _goal_totals_cache[project_id] = (now, totals)
    return totals
