"""Create the Invoices database in Notion.

An invoice records a decision *about* logged hours — "for July we billed Auter
138 of the 142 hours tracked" — without ever rewriting a Time Entry. One row per
(project, month): saving the same month again corrects that row rather than
filing a second bill.

`Hours tracked` is stored alongside `Hours billed` on purpose: it's what lets
the invoice list notice that a month's logged hours moved after it was invoiced,
which happens every time someone logs late.

Idempotent: does nothing if databases.json already has the ids.
"""
from config import get_client, get_parent_page_id, load_db_ids, save_db_ids
from setup_databases import create_db


def invoice_props(projects_ds_id: str) -> dict:
    return {
        "Invoice": {"title": {}},
        "Project": {"relation": {"data_source_id": projects_ds_id, "single_property": {}}},
        # the 1st of the month, kept as a real date so Notion can sort and
        # filter it — a "July 2026" text column can do neither
        "Month": {"date": {}},
        "Hours tracked": {"number": {"format": "number"}},
        "Hours billed": {"number": {"format": "number"}},
        # July's invoice is usually cut in August, so this is its own date
        # rather than the row's created time
        "Issued": {"date": {}},
        "Saved by": {"people": {}},
        "Note": {"rich_text": {}},
    }


def main() -> None:
    notion = get_client()
    ids = load_db_ids()

    if ids.get("invoices_ds_id"):
        print(f"Invoices database already exists (ds {ids['invoices_ds_id']}) — nothing to do.")
        return

    projects_ds = ids.get("projects_ds_id")
    if not projects_ds:
        raise SystemExit("Run src/setup_databases.py first — Invoices relates to Projects.")

    print("Creating Invoices database…")
    inv_db, inv_ds = create_db(notion, get_parent_page_id(), "Invoices",
                               invoice_props(projects_ds))
    print(f"  -> db {inv_db} / ds {inv_ds}")

    ids.update(invoices_db_id=inv_db, invoices_ds_id=inv_ds)
    save_db_ids(ids)
    print("Saved ids to databases.json.")
    print("On Render, set INVOICES_DB_ID and INVOICES_DS_ID to these values.")


if __name__ == "__main__":
    main()
