"""Create the People database in Notion and seed it from workspace members.

The People database is the roster the web app shows (assignments columns,
schedule rows, person dropdowns): curate it in Notion — delete a row to hide
someone, retitle a row to rename them, untick Active to keep them but hide
them. Each row links the real Notion user via the Person property so the app
can keep writing people properties elsewhere.

Idempotent: skips creation if databases.json already has the ids, and seeding
skips members who already have a row.
"""
from config import get_client, get_parent_page_id, load_db_ids, save_db_ids
from setup_databases import create_db

PEOPLE_PROPS = {
    "Name": {"title": {}},
    "Person": {"people": {}},
    "Active": {"checkbox": {}},
}


def list_workspace_members(notion) -> list[dict]:
    members = []
    start = None
    while True:
        res = notion.users.list(start_cursor=start, page_size=100) if start else notion.users.list(page_size=100)
        members += [u for u in res["results"] if u.get("type") == "person"]
        if not res.get("has_more"):
            break
        start = res["next_cursor"]
    return members


def seeded_user_ids(notion, people_ds: str) -> set:
    ids = set()
    kwargs = {"data_source_id": people_ds, "page_size": 100}
    while True:
        res = notion.data_sources.query(**kwargs)
        for row in res["results"]:
            for p in row["properties"].get("Person", {}).get("people", []):
                ids.add(p["id"])
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return ids


def main() -> None:
    notion = get_client()
    ids = load_db_ids()

    if ids.get("people_ds_id"):
        people_db, people_ds = ids["people_db_id"], ids["people_ds_id"]
        print(f"People database already exists (ds {people_ds}) — seeding only.")
    else:
        print("Creating People database…")
        people_db, people_ds = create_db(notion, get_parent_page_id(), "People", PEOPLE_PROPS)
        print(f"  -> db {people_db} / ds {people_ds}")
        ids.update({"people_db_id": people_db, "people_ds_id": people_ds})
        save_db_ids(ids)

    already = seeded_user_ids(notion, people_ds)
    added = 0
    for u in list_workspace_members(notion):
        if u["id"] in already:
            continue
        name = u.get("name") or (u.get("person") or {}).get("email") or "(unnamed)"
        notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": people_ds},
            properties={
                "Name": {"title": [{"type": "text", "text": {"content": name}}]},
                "Person": {"people": [{"object": "user", "id": u["id"]}]},
                "Active": {"checkbox": True},
            },
        )
        added += 1
        print(f"  + {name}")
    print(f"\nDone: {added} added, {len(already)} already present.")
    print("On Render, set PEOPLE_DS_ID to the ds id above (dashboard → Environment).")


if __name__ == "__main__":
    main()
