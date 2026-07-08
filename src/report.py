"""Summarize logged hours.

Usage:
    python src/report.py                 # all entries, grouped by person then project
    python src/report.py --by project    # grouped by project
    python src/report.py --since 2026-07-01
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from config import get_client, load_db_ids


def iter_entries(notion, ds_id: str, since: str | None):
    kwargs = {"data_source_id": ds_id, "page_size": 100}
    if since:
        kwargs["filter"] = {"property": "Date", "date": {"on_or_after": since}}
    while True:
        res = notion.data_sources.query(**kwargs)
        for row in res["results"]:
            yield row
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]


def read_entry(row):
    props = row["properties"]
    # Prefer an explicit Person (if that property still exists), else fall back to
    # who submitted the row (form submitter / "Logged by"), ignoring the API bot.
    people = props.get("Person", {}).get("people", [])
    if people:
        person = people[0]["name"]
    else:
        creator = props.get("Logged by", {}).get("created_by", {})
        person = creator.get("name") if creator.get("type") == "person" else "(unassigned)"
        person = person or "(unassigned)"
    rel = props["Project"]["relation"]
    project = rel[0]["id"] if rel else "(none)"  # id; name resolved lazily below
    hours = props["Hours"]["number"] or 0
    return person, project, hours


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--by", choices=["person", "project"], default="person")
    p.add_argument("--since")
    args = p.parse_args()

    notion = get_client()
    ids = load_db_ids()

    # Resolve project ids -> names once.
    proj_names = {}
    for row in iter_entries(notion, ids["projects_ds_id"], None):
        t = row["properties"]["Name"]["title"]
        proj_names[row["id"]] = t[0]["plain_text"] if t else "(untitled)"

    totals = defaultdict(float)
    grand = 0.0
    for row in iter_entries(notion, ids["time_entries_ds_id"], args.since):
        person, project_id, hours = read_entry(row)
        project = proj_names.get(project_id, "(none)")
        key = person if args.by == "person" else project
        totals[key] += hours
        grand += hours

    label = args.by.capitalize()
    print(f"\nHours by {label}" + (f" since {args.since}" if args.since else "") + ":")
    print("-" * 32)
    for key, hrs in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {key:<24} {hrs:>6.2f}")
    print("-" * 32)
    print(f"  {'TOTAL':<24} {grand:>6.2f}\n")


if __name__ == "__main__":
    main()
