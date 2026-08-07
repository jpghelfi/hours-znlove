"""Create the Absences database in Notion.

An absence is a stretch of days someone isn't working — "out from the 10th to
the 14th, holiday". One row holds the whole stretch rather than one row per
day: the reason is written once, deleting the absence is one click, and Notion
can draw the block on a timeline view. The web app expands a row into weekdays
when it counts days, so a Mon–Fri absence over a weekend still costs 5.

`Days` is stored as well as the dates so the Notion table shows what an absence
costs without anyone doing the arithmetic by hand.

Idempotent: does nothing if databases.json already has the ids.
"""
from config import get_client, get_parent_page_id, load_db_ids, save_db_ids
from setup_databases import create_db

ABSENCE_PROPS = {
    "Absence": {"title": {}},
    "Person": {"people": {}},
    # one date property carrying start *and* end, so a range is a single value
    # Notion can filter, sort and lay out on a calendar
    "Dates": {"date": {}},
    "Days": {"number": {"format": "number"}},  # weekdays covered
    "Reason": {"rich_text": {}},
}


def main() -> None:
    notion = get_client()
    ids = load_db_ids()

    if ids.get("absences_ds_id"):
        print(f"Absences database already exists (ds {ids['absences_ds_id']}) — nothing to do.")
        return

    print("Creating Absences database…")
    abs_db, abs_ds = create_db(notion, get_parent_page_id(), "Absences", ABSENCE_PROPS)
    print(f"  -> db {abs_db} / ds {abs_ds}")

    ids.update(absences_db_id=abs_db, absences_ds_id=abs_ds)
    save_db_ids(ids)
    print("Saved ids to databases.json.")
    print("On Render, set ABSENCES_DB_ID and ABSENCES_DS_ID to these values.")


if __name__ == "__main__":
    main()
