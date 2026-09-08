"""Tick `Approves absences` on the named People rows.

    ./.venv/bin/python src/set_absence_approvers.py "Zarco Nontol" "Juan Pablo Ghelfi"

Who may approve time off is curated in Notion, like every other kind of access
here — its own checkbox rather than the Admin one, because there are six admins
and two approvers. This script is only the first tick: after it, the column is
edited in Notion like `Active` and `Admin`.

It **unticks nobody**. Revoking is a deliberate act in Notion, and a script that
silently cleared everyone not named on the command line would make one typo an
outage of the queue. Idempotent: a row already ticked is left alone and said so.

Names are matched against the row title, case-insensitively.
"""
import sys

from config import get_client, load_db_ids

APPROVER_PROP = "Approves absences"


def _title(props: dict) -> str:
    title = props.get("Name", {}).get("title") or []
    return "".join(t.get("plain_text", "") for t in title).strip()


def main(names: list) -> int:
    if not names:
        print(__doc__)
        return 2
    notion = get_client()
    ids = load_db_ids()
    people_ds = ids.get("people_ds_id")
    if not people_ds:
        print("No People database configured (people_ds_id). Run setup_people_db.py first.")
        return 1

    # the column has to exist before it can be ticked — the same backfill the
    # web app does on boot (ops.ensure_approver_property)
    ds = notion.data_sources.retrieve(people_ds)
    if APPROVER_PROP not in ds["properties"]:
        notion.data_sources.update(people_ds, properties={APPROVER_PROP: {"checkbox": {}}})
        print(f"+ added the {APPROVER_PROP} checkbox to the People db")

    wanted = {n.strip().lower() for n in names if n.strip()}
    rows, kwargs = [], {"data_source_id": people_ds, "page_size": 100}
    while True:
        res = notion.data_sources.query(**kwargs)
        rows += res["results"]
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]

    seen = set()
    for row in rows:
        name = _title(row["properties"])
        if name.lower() not in wanted:
            continue
        seen.add(name.lower())
        if row["properties"].get(APPROVER_PROP, {}).get("checkbox"):
            print(f"  = {name} already approves absences")
            continue
        notion.pages.update(row["id"], properties={APPROVER_PROP: {"checkbox": True}})
        print(f"  + {name} can now approve absences")

    for missing in sorted(wanted - seen):
        print(f"  ! no People row titled {missing!r} — nothing ticked for them")

    ticked = [_title(r["properties"]) for r in rows
              if r["properties"].get(APPROVER_PROP, {}).get("checkbox")
              or _title(r["properties"]).lower() in seen]
    print("\nApprovers now: " + ", ".join(sorted(set(ticked))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
