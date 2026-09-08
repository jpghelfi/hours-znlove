"""Create the Plan items database in Notion.

A plan item is one thing a project intends to do — a feature, a task, a bug,
a milestone — with dates, an estimate, a status and an owner. The web app's
/plan page draws a project's items as a timeline (bars on days) and as a
tickets list, and can hand the same rows to a client as a PDF or a live,
login-free link.

Its own database rather than dated Goals (goals are standing buckets that
feed the reports; a week's plan is a dozen day-long rows that would drown
them) and rather than the ticket boards other teams own (whose columns they
rename). `Dates` is one range property, like Absences, so Notion's own
timeline view draws the plan too. Empty `Dates` = unscheduled, start = end =
a milestone. `Estimate`: empty is unestimated, 0 is "no hours" — the same
rule as budgets and goals. Everything client-facing is decided by the app's
share strip, not by this schema; `Internal` keeps a row off the share page.

Idempotent: does nothing if databases.json already has the ids.
"""
from __future__ import annotations

from config import get_client, get_parent_page_id, load_db_ids, save_db_ids
from setup_databases import create_db

STATUSES = [
    {"name": "Backlog", "color": "gray"},
    {"name": "Planned", "color": "blue"},
    {"name": "In progress", "color": "yellow"},
    {"name": "Blocked", "color": "red"},
    {"name": "Done", "color": "green"},
    {"name": "Dropped", "color": "default"},
]
TYPES = [
    {"name": "Feature", "color": "purple"},
    {"name": "Task", "color": "blue"},
    {"name": "Bug", "color": "red"},
    {"name": "Milestone", "color": "orange"},
]


def plan_props(projects_ds_id: str, goals_ds_id: str | None) -> dict:
    props = {
        "Item": {"title": {}},
        "Project": {"relation": {"data_source_id": projects_ds_id, "single_property": {}}},
        "Dates": {"date": {}},
        "Status": {"select": {"options": STATUSES}},
        "Type": {"select": {"options": TYPES}},
        "Estimate": {"number": {"format": "number"}},
        "Owner": {"people": {}},
        "Ticket URL": {"url": {}},
        "Ticket": {"rich_text": {}},
        "Note": {"rich_text": {}},
        "Internal": {"checkbox": {}},
        "Order": {"number": {"format": "number"}},
    }
    if goals_ds_id:
        props["Goal"] = {"relation": {"data_source_id": goals_ds_id, "single_property": {}}}
    return props


def main() -> None:
    notion = get_client()
    ids = load_db_ids()
    if ids.get("plan_ds_id"):
        print(f"Plan items database already exists (ds {ids['plan_ds_id']}) — nothing to do.")
        return
    projects_ds = ids.get("projects_ds_id")
    if not projects_ds:
        raise SystemExit("Run src/setup_databases.py first — Plan items relates to Projects.")
    print("Creating Plan items database…")
    db, ds = create_db(notion, get_parent_page_id(), "Plan items",
                       plan_props(projects_ds, ids.get("goals_ds_id")))
    print(f"  -> db {db} / ds {ds}")
    # a self-relation can't be declared at creation (the target doesn't exist
    # yet) — added now so v2's dependency arrows have somewhere to read from
    try:
        notion.data_sources.update(ds, properties={
            "Blocked by": {"relation": {"data_source_id": ds, "single_property": {}}}})
    except Exception as e:  # noqa: BLE001
        print(f"  (could not add the Blocked by self-relation: {e})")
    ids.update(plan_db_id=db, plan_ds_id=ds)
    save_db_ids(ids)
    print("Saved ids to databases.json.")
    print("On Render, set PLAN_DB_ID and PLAN_DS_ID to these values.")


if __name__ == "__main__":
    main()
