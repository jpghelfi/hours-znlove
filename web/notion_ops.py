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


def list_projects(active_only: bool = True) -> list[dict]:
    projects = []
    kwargs = {"data_source_id": PROJECTS_DS, "page_size": 100}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            title = row["properties"]["Name"]["title"]
            name = title[0]["plain_text"] if title else "(untitled)"
            active = row["properties"].get("Active", {}).get("checkbox", True)
            if active_only and not active:
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


def week_grid(monday: dt.date) -> dict:
    """Build the Mon–Fri grid: rows keyed by (person, project), each with per-day hours."""
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
        project_id = rel[0]["id"]
        person_id, person_name = _row_person(props)
        date = props["Date"]["date"]["start"][:10] if props["Date"]["date"] else None
        hours = props["Hours"]["number"] or 0
        if date not in day_isos:
            continue
        key = (person_id or "none", project_id)
        row = rows.setdefault(key, {
            "person_id": person_id, "person_name": person_name,
            "project_id": project_id, "project_name": pname_map.get(project_id, "(none)"),
            "cells": {iso: 0.0 for iso in day_isos},
        })
        row["cells"][date] += hours

    # order rows by person then project
    ordered = sorted(rows.values(), key=lambda r: (r["person_name"].lower(), r["project_name"].lower()))
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
