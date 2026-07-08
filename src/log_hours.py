"""Log a time entry manually.

Usage:
    python src/log_hours.py --person "Jane" --project "Mobile App" \
        --hours 2.5 --date 2026-07-08 --desc "Built the login screen"

--date defaults to today. --person matches a Notion workspace member by name
(case-insensitive substring); omit it to leave Person blank.
"""
import argparse
import datetime as dt

from config import get_client, load_db_ids


def find_user_id(notion, name: str):
    name_l = name.lower()
    for user in notion.users.list()["results"]:
        if user.get("type") == "person" and name_l in (user.get("name") or "").lower():
            return user["id"], user["name"]
    raise SystemExit(f'No workspace member matches "{name}".')


def find_project_page_id(notion, projects_ds_id: str, name: str):
    res = notion.data_sources.query(
        data_source_id=projects_ds_id,
        filter={"property": "Name", "title": {"contains": name}},
    )
    if not res["results"]:
        raise SystemExit(f'No project matches "{name}". Add it with seed_projects.py.')
    page = res["results"][0]
    title = page["properties"]["Name"]["title"][0]["plain_text"]
    return page["id"], title


def main() -> None:
    p = argparse.ArgumentParser(description="Log a time entry to Notion.")
    p.add_argument("--person")
    p.add_argument("--project", required=True)
    p.add_argument("--hours", type=float, required=True)
    p.add_argument("--date", default=dt.date.today().isoformat())
    p.add_argument("--desc", default="")
    args = p.parse_args()

    notion = get_client()
    ids = load_db_ids()

    project_id, project_name = find_project_page_id(notion, ids["projects_ds_id"], args.project)

    props = {
        "Entry": {"title": [{"text": {"content": f"{project_name} — {args.date}"}}]},
        "Project": {"relation": [{"id": project_id}]},
        "Date": {"date": {"start": args.date}},
        "Hours": {"number": args.hours},
        "Description": {"rich_text": [{"text": {"content": args.desc}}]},
    }

    if args.person:
        user_id, user_name = find_user_id(notion, args.person)
        props["Person"] = {"people": [{"id": user_id}]}
    else:
        user_name = "(unassigned)"

    notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": ids["time_entries_ds_id"]},
        properties=props,
    )
    print(f"Logged {args.hours}h on {args.date} — {project_name} — {user_name}")


if __name__ == "__main__":
    main()
