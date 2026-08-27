"""Create the Goals database in Notion.

A goal is a named bucket of work inside a project — "New homepage",
"Maintenance" — that logged hours get filed under, so a project's month reads
as *what it went into* rather than only who spent it and when.

It gets a database of its own rather than a select column on Time Entries for
two reasons: a Notion select is global to the database (one option list across
every project, and Notion invents an option for any name it doesn't know), and
a goal carries more than a name — a target, a lifetime, a status.

`Target basis` is what makes a goal reusable month after month:

  * `Total`     — "80 h for the new homepage", spent once, then Done.
  * `Per month` — "10 h of maintenance a month", the same row collecting hours
                  from January and December alike. Never Done.

Idempotent: does nothing if databases.json already has the ids.
"""
from config import get_client, get_parent_page_id, load_db_ids, save_db_ids
from setup_databases import create_db


def goal_props(projects_ds_id: str) -> dict:
    return {
        "Goal": {"title": {}},
        "Project": {"relation": {"data_source_id": projects_ds_id, "single_property": {}}},
        # Empty is not 0, exactly as with a project's Monthly budget: empty
        # means "untargeted", 0 would mean "no hours allowed here at all".
        "Target hours": {"number": {"format": "number"}},
        "Target basis": {"select": {"options": [
            {"name": "Total", "color": "blue"},
            {"name": "Per month", "color": "purple"},
        ]}},
        "Status": {"select": {"options": [
            {"name": "Open", "color": "green"},
            {"name": "Done", "color": "gray"},
            {"name": "Dropped", "color": "red"},
        ]}},
        # A standing goal leaves Due empty — that is what "it never ends" looks
        # like in the data, and what keeps it in the picker forever.
        "Started": {"date": {}},
        "Due": {"date": {}},
        "Note": {"rich_text": {}},
    }


def main() -> None:
    notion = get_client()
    ids = load_db_ids()

    if ids.get("goals_ds_id"):
        print(f"Goals database already exists (ds {ids['goals_ds_id']}) — nothing to do.")
        return

    projects_ds = ids.get("projects_ds_id")
    if not projects_ds:
        raise SystemExit("Run src/setup_databases.py first — Goals relates to Projects.")

    print("Creating Goals database…")
    goals_db, goals_ds = create_db(notion, get_parent_page_id(), "Goals",
                                   goal_props(projects_ds))
    print(f"  -> db {goals_db} / ds {goals_ds}")

    ids["goals_db_id"] = goals_db
    ids["goals_ds_id"] = goals_ds
    save_db_ids(ids)
    print("\nSaved ids to databases.json.")
    print("On Render, set GOALS_DS_ID (and GOALS_DB_ID) to these values.")
    print("The Goal relation on Time Entries is added by the web app at startup.")


if __name__ == "__main__":
    main()
