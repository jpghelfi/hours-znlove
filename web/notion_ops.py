"""Notion data operations for the web app.

Notion remains the source of truth. This module reads people/projects and
reads/writes Time Entries via the 2025-09-03 data-source API.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

# reuse the existing client/config from src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import get_client, load_db_ids  # noqa: E402

_notion = get_client()
_ids = load_db_ids()
TIME_DS = _ids["time_entries_ds_id"]
PROJECTS_DS = _ids["projects_ds_id"]


def ensure_person_property() -> None:
    """Make sure Time Entries has a Person (people) property; add it if missing.

    The web form self-selects a person, so we store it explicitly (Notion's
    'Logged by' only captures the submitter inside Notion's own UI/forms).
    """
    ds = _notion.data_sources.retrieve(TIME_DS)
    if "Person" not in ds["properties"]:
        _notion.data_sources.update(TIME_DS, properties={"Person": {"people": {}}})


# ---- reads -------------------------------------------------------------

def list_people() -> list[dict]:
    """Workspace members (real people, not bots), for the self-select dropdown."""
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
    people.sort(key=lambda p: p["name"].lower())
    return people


def get_user(user_id: str) -> dict:
    """Resolve a Notion user id to {id, name, email} using the integration token."""
    u = _notion.users.retrieve(user_id)
    return {
        "id": u["id"],
        "name": u.get("name") or "(unnamed)",
        "email": (u.get("person") or {}).get("email"),
        "avatar": u.get("avatar_url"),
    }


def list_projects(active_only: bool = True, member_of: str | None = None) -> list[dict]:
    """List projects. If member_of (a Notion user id) is given, return only
    projects that user is a member of (the People property includes them)."""
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
            if member_of is not None:
                members = [p["id"] for p in props.get("People", {}).get("people", [])]
                if member_of not in members:
                    continue
            projects.append({"id": row["id"], "name": name})
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
    """Allocations grid: rows = person × project, columns = n_weeks Mondays."""
    weeks = [(start_monday + dt.timedelta(weeks=i)).isoformat() for i in range(n_weeks)]
    pname = _project_name_map()
    rows: dict = {}
    kwargs = {"data_source_id": ALLOC_DS, "page_size": 100, "filter": {"and": [
        {"property": "Week", "date": {"on_or_after": weeks[0]}},
        {"property": "Week", "date": {"on_or_before": weeks[-1]}},
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
            week = props["Week"]["date"]["start"][:10]
            if week not in weeks:
                continue
            key = (pid, rel[0]["id"])
            r = rows.setdefault(key, {
                "person_id": pid, "person_name": pname_person,
                "project_id": rel[0]["id"], "project_name": pname.get(rel[0]["id"], "(none)"),
                "cells": {w: 0.0 for w in weeks},
            })
            r["cells"][week] += props["Hours"]["number"] or 0
    # noqa: pagination
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]

    ordered = sorted(rows.values(), key=lambda r: (r["person_name"].lower(), r["project_name"].lower()))
    # per-person weekly totals for the capacity heat map
    people_totals: dict = {}
    for r in ordered:
        pt = people_totals.setdefault(r["person_name"], {w: 0.0 for w in weeks})
        for w in weeks:
            pt[w] += r["cells"][w]
    return {"weeks": weeks, "rows": ordered, "people_totals": people_totals}


def set_allocation(person_id: str, project_id: str, week: str, hours: float) -> dict:
    """Upsert the (person, project, week) allocation. 0 deletes it."""
    res = _notion.data_sources.query(
        data_source_id=ALLOC_DS,
        filter={"and": [
            {"property": "Week", "date": {"equals": week}},
            {"property": "Project", "relation": {"contains": project_id}},
        ]})
    match = None
    for row in res["results"]:
        people = row["properties"]["Person"]["people"]
        if people and people[0]["id"] == person_id:
            match = row
            break
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
                "Allocation": {"title": [{"text": {"content": f"{pmap.get(project_id,'?')} — {week}"}}]},
                "Person": {"people": [{"id": person_id}]},
                "Project": {"relation": [{"id": project_id}]},
                "Week": {"date": {"start": week}},
                "Hours": {"number": hours},
            })
    return {"ok": True, "hours": hours}


def planned_between(date_from: str, date_to: str, person_id: str | None = None) -> dict:
    """Planned hours by project for allocations whose Week falls in the range."""
    pname = _project_name_map()
    out: dict = {}
    kwargs = {"data_source_id": ALLOC_DS, "page_size": 100, "filter": {"and": [
        {"property": "Week", "date": {"on_or_after": date_from}},
        {"property": "Week", "date": {"on_or_before": date_to}},
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
            if not rel:
                continue
            name = pname.get(rel[0]["id"], "(none)")
            out[name] = out.get(name, 0) + (props["Hours"]["number"] or 0)
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return out


def set_cell(person_id: str, project_id: str, date: str, hours: float) -> dict:
    """Upsert the (person, project, date) cell to `hours`. 0/None deletes the entry."""
    res = _notion.data_sources.query(
        data_source_id=TIME_DS,
        filter={"and": [
            {"property": "Date", "date": {"equals": date}},
            {"property": "Project", "relation": {"contains": project_id}},
        ]},
    )
    match = None
    for row in res["results"]:
        pid, _ = _row_person(row["properties"])
        if pid == person_id:
            match = row
            break

    if not hours:  # 0, None -> remove
        if match:
            _notion.pages.update(match["id"], archived=True)
        return {"ok": True, "hours": 0}

    if match:
        _notion.pages.update(match["id"], properties={
            "Hours": {"number": hours},
            "Person": {"people": [{"id": person_id}]},
        })
    else:
        create_entry(person_id, project_id, date, hours)
    return {"ok": True, "hours": hours}
