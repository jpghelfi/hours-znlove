"""Add projects to the Projects database.

Usage:
    python src/seed_projects.py "Website Redesign" "Mobile App" "Internal Tools"
"""
import sys

from config import get_client, load_db_ids


def add_project(notion, projects_ds_id: str, name: str) -> None:
    notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": projects_ds_id},
        properties={
            "Name": {"title": [{"text": {"content": name}}]},
            "Active": {"checkbox": True},
        },
    )
    print(f"  + {name}")


def main() -> None:
    names = sys.argv[1:]
    if not names:
        raise SystemExit('Usage: python src/seed_projects.py "Project A" "Project B" ...')
    notion = get_client()
    projects_ds_id = load_db_ids()["projects_ds_id"]
    print("Adding projects…")
    for name in names:
        add_project(notion, projects_ds_id, name)
    print("Done.")


if __name__ == "__main__":
    main()
