"""Create the Projects and Time Entries databases in Notion.

Uses the 2025-09-03 Notion API (notion-client >= 3): a database wraps a
*data source*, schema goes in `initial_data_source`, and relations point at
data source ids. Run once; ids land in databases.json.
"""
from config import get_client, get_parent_page_id, save_db_ids

PROJECT_PROPS = {
    "Name": {"title": {}},
    "Active": {"checkbox": {}},
    "Client": {"rich_text": {}},
}


def time_entry_props(projects_ds_id: str) -> dict:
    return {
        "Entry": {"title": {}},
        "Person": {"people": {}},
        "Project": {"relation": {"data_source_id": projects_ds_id, "single_property": {}}},
        "Date": {"date": {}},
        "Hours": {"number": {"format": "number"}},
        "Description": {"rich_text": {}},
    }


def create_db(notion, parent_page_id: str, title: str, properties: dict) -> tuple[str, str]:
    """Create a database and return (database_id, data_source_id)."""
    db = notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": title}}],
        initial_data_source={"properties": properties},
    )
    return db["id"], db["data_sources"][0]["id"]


def main() -> None:
    notion = get_client()
    parent = get_parent_page_id()

    print("Creating Projects database…")
    projects_db, projects_ds = create_db(notion, parent, "Projects", PROJECT_PROPS)
    print(f"  -> db {projects_db} / ds {projects_ds}")

    print("Creating Time Entries database…")
    entries_db, entries_ds = create_db(notion, parent, "Time Entries", time_entry_props(projects_ds))
    print(f"  -> db {entries_db} / ds {entries_ds}")

    save_db_ids({
        "projects_db_id": projects_db,
        "projects_ds_id": projects_ds,
        "time_entries_db_id": entries_db,
        "time_entries_ds_id": entries_ds,
    })
    print("\nSaved ids to databases.json. Next: python src/seed_projects.py \"Project A\" \"Project B\"")


if __name__ == "__main__":
    main()
