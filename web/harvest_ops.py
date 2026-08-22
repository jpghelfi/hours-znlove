"""Harvest sync, web side: read Notion, plan, write Notion.

The matching and the upsert rules live in src/harvest_sync.py (pure, no I/O)
and the Harvest calls in src/harvest_api.py, so the CLI and this page can't
drift apart. What is here is the Notion half: the properties the sync needs,
the reads that feed a plan, and the writes that apply one.

Two rules worth keeping in view while reading this file:

* **`preview()` writes nothing and reads everything.** It does the same
  existing-rows read `apply()` does, which is what lets it say "3 already
  exist, unchanged" and "2 collide with hand-logged rows" — the CLI's
  --dry-run skipped that read and could only ever say "would write N".
* **`apply()` re-plans from Harvest and Notion.** The browser sends a period
  and some options, never a plan — the same reason set_entry_hours re-checks
  the page it is handed.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import harvest_api            # noqa: E402
import harvest_sync           # noqa: E402
import notion_ops as ops      # noqa: E402
from notion_ops import _notion, _write_lock, TIME_DS, PEOPLE_DS, PROJECTS_DS  # noqa: E402

MAX_RANGE_DAYS = 31           # a month at a time; longer ranges are chunked by the browser
SOURCE_PROP = "Source"
SYNC_PROP = "Harvest Sync"
PERSON_ID_PROP = "Harvest User Id"
PROJECT_IDS_PROP = "Harvest Project"


def enabled() -> bool:
    return harvest_api.enabled()


def ensure_harvest_properties() -> None:
    """Add the four properties the sync needs, if they aren't there yet.

    Same startup-bootstrap pattern as ensure_person_property /
    ensure_task_properties / ensure_admin_property: the deploy shouldn't need
    someone to add columns in the Notion UI first, and re-running is a no-op.

    `Source` is a select rather than the old marker line in the Description,
    because a select can be filtered in the Notion query — which is what makes
    finding rows deleted on the Harvest side affordable instead of a full scan.
    """
    if not enabled():
        return
    try:
        ds = _notion.data_sources.retrieve(TIME_DS)
        missing = {}
        if SOURCE_PROP not in ds["properties"]:
            missing[SOURCE_PROP] = {"select": {"options": [
                {"name": harvest_sync.SOURCE, "color": "orange"}]}}
        if SYNC_PROP not in ds["properties"]:
            missing[SYNC_PROP] = {"rich_text": {}}
        if missing:
            _notion.data_sources.update(TIME_DS, properties=missing)
    except Exception:
        logging.exception("Could not add the Harvest properties to Time Entries")
    for ds_id, prop, spec in ((PEOPLE_DS, PERSON_ID_PROP, {"number": {}}),
                              (PROJECTS_DS, PROJECT_IDS_PROP, {"rich_text": {}})):
        if not ds_id:
            continue
        try:
            ds = _notion.data_sources.retrieve(ds_id)
            if prop not in ds["properties"]:
                _notion.data_sources.update(ds_id, properties={prop: spec})
        except Exception:
            logging.exception("Could not add %s", prop)


# ---- reads -------------------------------------------------------------

def _harvest_ids(props: dict) -> list[int]:
    """The Harvest project ids on a Notion project row: "123, 456"."""
    text = "".join(t.get("plain_text", "") for t in props.get(PROJECT_IDS_PROP, {}).get("rich_text", []))
    out = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def roster() -> list[dict]:
    """The People roster plus each row's explicit Harvest user id.

    ops.list_people() is the roster everything else uses, but it doesn't carry
    the mapping column, so the People db is read once more here and joined on
    the Notion user id. `page_id` rides along because writing a mapping needs
    the People *row*, not the person.
    """
    people = {p["id"]: dict(p) for p in ops.list_people()}
    if not PEOPLE_DS:
        return list(people.values())
    kwargs = {"data_source_id": PEOPLE_DS, "page_size": 100}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            linked = props.get("Person", {}).get("people", [])
            if not linked or linked[0]["id"] not in people:
                continue
            person = people[linked[0]["id"]]
            person["page_id"] = row["id"]
            hid = props.get(PERSON_ID_PROP, {}).get("number")
            if hid is not None:
                person["harvest_id"] = int(hid)
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return list(people.values())


def projects() -> list[dict]:
    """Active projects with members and their explicit Harvest project ids."""
    out = []
    kwargs = {"data_source_id": PROJECTS_DS, "page_size": 100}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            if not props.get("Active", {}).get("checkbox", True):
                continue
            title = props["Name"]["title"]
            out.append({
                "id": row["id"],
                "name": title[0]["plain_text"] if title else "(untitled)",
                "member_ids": [p["id"] for p in props.get("People", {}).get("people", [])],
                "harvest_ids": _harvest_ids(props),
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    out.sort(key=lambda p: p["name"].lower())
    return out


def existing_rows(date_from: str, date_to: str) -> dict:
    """(person_id, project_id, date) -> [{page_id, hours, description, source}].

    Every entry in the range, not just synced ones: the plan has to see a
    hand-logged row in order to refuse to touch it.
    """
    out: dict = {}
    kwargs = {"data_source_id": TIME_DS, "page_size": 100, "filter": {"and": [
        {"property": "Date", "date": {"on_or_after": date_from}},
        {"property": "Date", "date": {"on_or_before": date_to}},
    ]}}
    while True:
        res = _notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            people = props.get("Person", {}).get("people", [])
            rel = props.get("Project", {}).get("relation") or []
            date = props.get("Date", {}).get("date")
            if not (people and rel and date):
                continue
            desc = props.get("Description", {}).get("rich_text") or []
            source = (props.get(SOURCE_PROP, {}).get("select") or {}).get("name")
            out.setdefault((people[0]["id"], rel[0]["id"], date["start"][:10]), []).append({
                "page_id": row["id"],
                "hours": props.get("Hours", {}).get("number") or 0,
                "description": "".join(t.get("plain_text", "") for t in desc),
                "source": source,
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return out


# ---- plan / apply ------------------------------------------------------

def _range_ok(date_from: str, date_to: str) -> tuple[dt.date, dt.date]:
    start, end = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
    if end < start:
        raise ValueError("that range ends before it starts")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"that range is longer than {MAX_RANGE_DAYS} days — sync it a week at a time")
    return start, end


def preview(date_from: str, date_to: str, opts: dict | None = None) -> dict:
    """What a sync of this range would do. Reads only — writes nothing."""
    _range_ok(date_from, date_to)
    entries = harvest_api.time_entries(date_from, date_to)
    full = dict(opts or {}, date_from=date_from, date_to=date_to)
    result = harvest_sync.plan(entries, roster(), projects(),
                               existing_rows(date_from, date_to), full)
    result["range"] = {"from": date_from, "to": date_to}
    return result


def apply(date_from: str, date_to: str, opts: dict | None = None) -> dict:
    """Write the plan for this range into Notion.

    The plan is recomputed here rather than accepted from the browser, so a
    stale tab (or a hand-edited request) can't write rows nobody previewed.

    `_write_lock` is taken **per row, not for the whole run** — the opposite of
    set_allocation_range. A hundred writes under one lock would freeze every
    weekly-grid save in the app for a minute, and each row here is
    independently idempotent, so fairness beats atomicity. The counters are
    built as it goes, so a failure halfway still reports what landed.
    """
    _range_ok(date_from, date_to)
    reconcile = bool((opts or {}).get("reconcile"))
    plan = preview(date_from, date_to, opts)
    if plan["totals"]["over_cap"]:
        raise ValueError(
            f"that range plans {plan['totals']['writes']} writes — more than the "
            f"{harvest_sync.MAX_SYNC_ROWS} one sync will make; do it a week at a time")

    counts = {"created": 0, "updated": 0, "unchanged": 0, "conflict": 0,
              "deleted": 0, "failed": 0}
    failures = []
    stamped = dt.date.today().isoformat()
    for row in plan["rows"]:
        if row["action"] in ("unchanged", "conflict"):
            counts[row["action"] if row["action"] == "conflict" else "unchanged"] += 1
            continue
        try:
            _write(row, stamped)
            counts["created" if row["action"] == "create" else "updated"] += 1
        except Exception as e:
            counts["failed"] += 1
            failures.append(f"{row['date']} {row['person']} / {row['project']}: {e}")
            logging.exception("Harvest sync failed on %s %s/%s",
                              row["date"], row["person"], row["project"])
    if reconcile:
        for row in plan["stale"]:
            try:
                with _write_lock:
                    _notion.pages.update(row["page_id"], archived=True)
                counts["deleted"] += 1
            except Exception as e:
                counts["failed"] += 1
                failures.append(f"{row['date']}: could not remove a stale row: {e}")
    return {"ok": True, "counts": counts, "failures": failures[:20],
            "totals": plan["totals"], "range": plan["range"],
            "unknown_people": plan["unknown_people"],
            "unmapped_projects": plan["unmapped_projects"],
            "ambiguous": plan["ambiguous"]}


def _write(row: dict, stamped: str) -> None:
    """Create or update one day row, holding the lock only for that row."""
    sync_note = '{"ids":%s,"hours":%s,"at":"%s"}' % (
        row["harvest_ids"][:40], row["hours"], stamped)
    props = {
        "Project": {"relation": [{"id": row["project_id"]}]},
        "Date": {"date": {"start": row["date"]}},
        "Hours": {"number": row["hours"]},
        "Description": {"rich_text": [
            {"text": {"content": harvest_sync.description(row["notes"])[:2000]}}]},
        "Person": {"people": [{"id": row["person_id"]}]},
        SOURCE_PROP: {"select": {"name": harvest_sync.SOURCE}},
        SYNC_PROP: {"rich_text": [{"text": {"content": sync_note[:2000]}}]},
    }
    with _write_lock:
        if row["page_id"]:
            _notion.pages.update(row["page_id"], properties=props)
        else:
            props["Entry"] = {"title": [
                {"text": {"content": f"{row['project']} — {row['date']}"}}]}
            _notion.pages.create(
                parent={"type": "data_source_id", "data_source_id": TIME_DS},
                properties=props)


# ---- mapping -----------------------------------------------------------

def set_person_harvest_id(page_id: str, harvest_id: int | None) -> dict:
    """Remember which Harvest user a roster row is. None clears it."""
    with _write_lock:
        page = _notion.pages.retrieve(page_id)
        if ops._bare((page.get("parent") or {}).get("data_source_id")) != ops._bare(PEOPLE_DS):
            raise ValueError("not a People row")
        _notion.pages.update(page_id, properties={
            PERSON_ID_PROP: {"number": int(harvest_id) if harvest_id else None}})
    return {"ok": True}


def set_project_harvest_ids(project_id: str, harvest_ids: list[int]) -> dict:
    """Remember which Harvest projects map onto a Notion project.

    A list, because one Notion project legitimately covers several
    budget-suffixed Harvest projects; adding is the normal case, so callers
    pass the full set they want stored.
    """
    ids = sorted({int(i) for i in harvest_ids if str(i).strip().isdigit()})
    with _write_lock:
        page = _notion.pages.retrieve(project_id)
        if ops._bare((page.get("parent") or {}).get("data_source_id")) != ops._bare(PROJECTS_DS):
            raise ValueError("not a project")
        _notion.pages.update(project_id, properties={
            PROJECT_IDS_PROP: {"rich_text": [{"text": {"content": ", ".join(str(i) for i in ids)}}] if ids else []}})
    return {"ok": True, "harvest_ids": ids}
