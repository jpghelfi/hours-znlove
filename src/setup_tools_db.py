"""Create the Tools database in Notion — the catalog of software the team uses.

One row per tool (Slack, Google account, Claude, Figma…). Who has it is a
`People` property on that row, the way a project's members are a `People`
property on the Projects row: an assignment carries nothing beyond "this person
has this tool", so it doesn't earn a database of its own.

`Category` is plain text, not a select — a Notion select invents an option for
any name it's handed, and the catalog's categories are whatever people type.
Retiring a tool unticks `Active` rather than deleting the row, so who had it
stays on record.

Idempotent: does nothing if databases.json already has the ids.
"""
from config import get_client, get_parent_page_id, load_db_ids, save_db_ids
from setup_databases import create_db

TOOL_PROPS = {
    "Tool": {"title": {}},
    "Category": {"rich_text": {}},
    "URL": {"url": {}},
    "Notes": {"rich_text": {}},
    "People": {"people": {}},
    "Active": {"checkbox": {}},
}


def main() -> None:
    notion = get_client()
    ids = load_db_ids()

    if ids.get("tools_ds_id"):
        print(f"Tools database already exists (ds {ids['tools_ds_id']}) — nothing to do.")
        return

    print("Creating Tools database…")
    db, ds = create_db(notion, get_parent_page_id(), "Tools", TOOL_PROPS)
    print(f"  -> db {db} / ds {ds}")

    ids.update(tools_db_id=db, tools_ds_id=ds)
    save_db_ids(ids)
    print("Saved ids to databases.json.")
    print("On Render, set TOOLS_DB_ID and TOOLS_DS_ID to these values.")


if __name__ == "__main__":
    main()
