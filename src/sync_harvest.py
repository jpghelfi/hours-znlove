"""Import Harvest time entries into the Notion Time Entries database.

Only znlove people are imported: a Harvest user is included only when their name
matches a row in the Notion **People** roster. Only *billable* time is imported by
default, and a person/project pair is only written when that person is assigned to
the project in Notion (the project's People property) — see --allow-unassigned.

Harvest entries are rolled up to one Notion row per (person, project, day); the
Description of every imported row starts with a "Harvest" marker line so imported
time is distinguishable from time logged in the app:

    Harvest
    BW-30, BGONCO-232

That marker is also what makes re-runs idempotent: a row whose description starts
with "Harvest" is *updated* on a re-run, and a day that already has a hand-logged
(non-Harvest) row is skipped and reported rather than double-counted.

Getting the input JSON — either works:

  * With Harvest API credentials (set HARVEST_ACCOUNT_ID + HARVEST_TOKEN in .env,
    from https://id.getharvest.com/developers), the script fetches the range itself:
        python src/sync_harvest.py --from 2026-07-01 --to 2026-07-31 --dry-run
  * Or feed it a saved Harvest API response (e.g. pulled through the Harvest MCP
    connector) — a JSON object with an "items"/"time_entries" list, or a bare list:
        python src/sync_harvest.py --from 2026-07-01 --to 2026-07-31 \
            --entries harvest_july.json --dry-run

Drop --dry-run to actually write to Notion.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import unicodedata
import urllib.request
from pathlib import Path

from config import get_client, load_db_ids

MARKER = "Harvest"

# Harvest project name -> Notion project name, for the pairs that plain
# normalized matching can't get right on its own. Anything not listed is matched
# by name (see match_project); unmatched Harvest projects are reported and skipped.
PROJECT_OVERRIDES = {
    "streamside: 7 additional parks": "Streamside 7 Additional parks",
    "bear website (internal)": "Bear Website",
}


# ---- helpers -----------------------------------------------------------

def norm(s: str) -> str:
    """Lowercase, strip accents, drop punctuation — for fuzzy name matching."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def name_tokens(s: str) -> set:
    return {w for w in norm(s).split() if len(w) > 2}


def fetch_harvest(date_from: str, date_to: str) -> list:
    """Pull time entries straight from the Harvest API (needs HARVEST_* env vars)."""
    account, token = os.environ.get("HARVEST_ACCOUNT_ID"), os.environ.get("HARVEST_TOKEN")
    if not (account and token):
        raise SystemExit(
            "No --entries file and no Harvest credentials: set HARVEST_ACCOUNT_ID and "
            "HARVEST_TOKEN in .env, or pass --entries with a saved Harvest response."
        )
    out, page = [], 1
    while True:
        url = (f"https://api.harvestapp.com/v2/time_entries?from={date_from}&to={date_to}"
               f"&per_page=2000&page={page}")
        req = urllib.request.Request(url, headers={
            "Harvest-Account-Id": account,
            "Authorization": f"Bearer {token}",
            "User-Agent": "hours-znlove sync",
        })
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        out.extend(data.get("time_entries", []))
        if not data.get("next_page"):
            return out
        page = data["next_page"]


def load_entries(path: str) -> list:
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        return data
    for key in ("items", "time_entries", "results"):
        if isinstance(data.get(key), list):
            return data[key]
    raise SystemExit(f"{path}: expected a list of Harvest time entries (or an 'items' key).")


def match_project(harvest_name: str, notion_projects: list) -> dict | None:
    """Map a Harvest project name onto a Notion project.

    Harvest names carry budget/year suffixes ("SaltWorks - OSS (40h)",
    "Streamside OSS - 60h (2026)"), so a Notion name counts as a match when its
    normalized form is a prefix of, or contained in, the normalized Harvest name.
    """
    hn = norm(harvest_name)
    override = PROJECT_OVERRIDES.get(hn)
    if override:
        return next((p for p in notion_projects if p["name"] == override), None)
    exact = [p for p in notion_projects if norm(p["name"]) == hn]
    if exact:
        return exact[0]
    cand = [p for p in notion_projects if hn.startswith(norm(p["name"]) + " ")
            or norm(p["name"]) in hn]
    if not cand:
        return None
    # Longest Notion name wins, so "Vital Signals - OSS (80h)" beats "Vital Signals".
    return max(cand, key=lambda p: len(p["name"]))


# ---- Notion side -------------------------------------------------------

def notion_people(notion, people_ds: str) -> list:
    """The znlove roster: active rows of the People db, with their Notion user id."""
    people, kwargs = [], {"data_source_id": people_ds, "page_size": 100}
    while True:
        res = notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            if not props.get("Active", {}).get("checkbox", True):
                continue
            users = props.get("Person", {}).get("people", [])
            if not users:
                continue
            title = props.get("Name", {}).get("title", [])
            name = title[0]["plain_text"] if title else users[0].get("name", "")
            people.append({"id": users[0]["id"], "name": name})
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return people


def notion_projects(notion, projects_ds: str) -> list:
    projects, kwargs = [], {"data_source_id": projects_ds, "page_size": 100}
    while True:
        res = notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            title = props["Name"]["title"]
            projects.append({
                "id": row["id"],
                "name": title[0]["plain_text"] if title else "(untitled)",
                "member_ids": [p["id"] for p in props.get("People", {}).get("people", [])],
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return projects


def existing_rows(notion, time_ds: str, date_from: str, date_to: str) -> dict:
    """(person_id, project_id, date) -> list of {page_id, hours, description, harvest}."""
    out = collections.defaultdict(list)
    kwargs = {"data_source_id": time_ds, "page_size": 100, "filter": {"and": [
        {"property": "Date", "date": {"on_or_after": date_from}},
        {"property": "Date", "date": {"on_or_before": date_to}},
    ]}}
    while True:
        res = notion.data_sources.query(**kwargs)
        for row in res["results"]:
            props = row["properties"]
            people = props.get("Person", {}).get("people", [])
            pid = people[0]["id"] if people else None
            rel = props["Project"]["relation"]
            date = props["Date"]["date"]
            if not (pid and rel and date):
                continue
            desc = props["Description"]["rich_text"]
            text = desc[0]["plain_text"] if desc else ""
            out[(pid, rel[0]["id"], date["start"][:10])].append({
                "page_id": row["id"],
                "hours": props["Hours"]["number"] or 0,
                "description": text,
                "harvest": text.startswith(MARKER),
            })
        if not res.get("has_more"):
            break
        kwargs["start_cursor"] = res["next_cursor"]
    return out


def write_row(notion, time_ds: str, existing: dict, person: dict, project: dict,
              date: str, hours: float, description: str) -> str:
    """Create or update one day row. Returns 'created' | 'updated' | 'unchanged'."""
    props = {
        "Project": {"relation": [{"id": project["id"]}]},
        "Date": {"date": {"start": date}},
        "Hours": {"number": round(hours, 2)},
        "Description": {"rich_text": [{"text": {"content": description[:2000]}}]},
        "Person": {"people": [{"id": person["id"]}]},
    }
    prior = [r for r in existing.get((person["id"], project["id"], date), []) if r["harvest"]]
    if prior:
        row = prior[0]
        if abs(row["hours"] - round(hours, 2)) < 0.005 and row["description"] == description:
            return "unchanged"
        notion.pages.update(page_id=row["page_id"], properties=props)
        return "updated"
    props["Entry"] = {"title": [{"text": {"content": f"{project['name']} — {date}"}}]}
    notion.pages.create(parent={"type": "data_source_id", "data_source_id": time_ds},
                        properties=props)
    return "created"


# ---- main --------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Sync Harvest time entries into Notion.")
    p.add_argument("--from", dest="date_from", required=True, help="start date YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", required=True, help="end date YYYY-MM-DD")
    p.add_argument("--entries", help="saved Harvest API response (JSON); omit to use the API")
    p.add_argument("--include-non-billable", action="store_true",
                   help="also import non-billable Harvest time (default: billable only)")
    p.add_argument("--nonbillable-project", action="append", default=[], metavar="NAME",
                   help="import non-billable time for this project too (repeatable). "
                        "Matches the Notion or the Harvest project name, e.g. "
                        "--nonbillable-project 'Bear Website'")
    p.add_argument("--allow-unassigned", action="store_true",
                   help="import person/project pairs even when the person isn't assigned "
                        "to that project in Notion (default: skip and report them)")
    p.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = p.parse_args()

    notion = get_client()
    ids = load_db_ids()
    if not ids.get("people_ds_id"):
        raise SystemExit("No People database configured — run src/setup_people_db.py first.")

    roster = notion_people(notion, ids["people_ds_id"])
    projects = notion_projects(notion, ids["projects_ds_id"])
    raw = load_entries(args.entries) if args.entries else fetch_harvest(args.date_from, args.date_to)

    # 1. filter: date range, billable, roster membership, mapped+assigned project
    by_token = {p["name"]: name_tokens(p["name"]) for p in roster}
    nonbillable_ok = {norm(n) for n in args.nonbillable_project}
    person_cache, project_cache = {}, {}
    skipped = collections.Counter()
    off_assignment, unmapped, non_roster = collections.Counter(), collections.Counter(), set()
    days = collections.defaultdict(lambda: {"hours": 0.0, "notes": []})

    for e in raw:
        date = e["spent_date"]
        if not (args.date_from <= date <= args.date_to):
            skipped["out of date range"] += 1
            continue
        huser = e["user"]["name"]
        if huser not in person_cache:
            toks = name_tokens(huser)
            hit = [r for r in roster if len(toks & by_token[r["name"]]) >= 2]
            person_cache[huser] = hit[0] if hit else None
        person = person_cache[huser]
        if not person:
            non_roster.add(huser)
            skipped["not on the znlove roster"] += 1
            continue

        hproj = e["project"]["name"]
        if hproj not in project_cache:
            project_cache[hproj] = match_project(hproj, projects)
        project = project_cache[hproj]
        if not project:
            unmapped[hproj] += e["hours"]
            skipped["no matching Notion project"] += 1
            continue

        # Billable is checked after the project is known, so --nonbillable-project
        # can let internal projects through by name. Harvest flags whole projects
        # non-billable (e.g. "Bear Website (Internal)"), so this is the only way
        # that time can ever be imported.
        if not (e.get("billable") or args.include_non_billable
                or norm(project["name"]) in nonbillable_ok or norm(hproj) in nonbillable_ok):
            skipped["non-billable"] += 1
            continue

        if person["id"] not in project["member_ids"]:
            off_assignment[(person["name"], project["name"])] += e["hours"]
            if not args.allow_unassigned:
                skipped["not assigned to the project"] += 1
                continue

        cell = days[(person["name"], person["id"], project["name"], project["id"], date)]
        cell["hours"] += e["hours"]
        note = (e.get("notes") or "").strip()
        if note and note not in cell["notes"]:
            cell["notes"].append(note)

    # 2. report what is going in
    total = sum(c["hours"] for c in days.values())
    scope = "all" if args.include_non_billable else "billable"
    if not args.include_non_billable and args.nonbillable_project:
        scope += " + non-billable on " + ", ".join(args.nonbillable_project)
    print(f"Harvest {args.date_from}..{args.date_to} ({scope}): {len(raw)} entries read -> "
          f"{len(days)} day rows, {total:.2f}h for "
          f"{len({k[1] for k in days})} people")
    for reason, n in skipped.most_common():
        print(f"  skipped {n:4d} Harvest entries — {reason}")
    if non_roster:
        print(f"  ({len(non_roster)} Harvest users are not on the roster — ignored)")
    for name, hours in unmapped.most_common():
        print(f"  !! no Notion project for Harvest project {name!r} ({hours:.2f}h)")
    for (person, project), hours in off_assignment.most_common():
        verb = "imported anyway" if args.allow_unassigned else "SKIPPED"
        print(f"  !! {person} is not assigned to {project} ({hours:.2f}h) — {verb}")

    # 3. write. Existing Notion rows are read once up front so a re-run updates
    #    its own rows instead of duplicating them.
    counts = collections.Counter()
    existing = {} if args.dry_run else existing_rows(
        notion, ids["time_entries_ds_id"], args.date_from, args.date_to)
    for (pname, pid, projname, projid, date), cell in sorted(days.items(), key=lambda kv: kv[0]):
        if round(cell["hours"], 2) <= 0:
            # Harvest keeps 0h entries (a timer started and cleared); in this app a
            # zero-hour cell means "no entry", so there is nothing to write.
            counts["zero"] += 1
            continue
        description = MARKER + ("\n" + ", ".join(cell["notes"]) if cell["notes"] else "")
        if args.dry_run:
            counts["planned"] += 1
            continue
        prior = existing.get((pid, projid, date), [])
        if prior and not any(r["harvest"] for r in prior):
            print(f"  !! {date} {pname} / {projname}: {sum(r['hours'] for r in prior)}h already "
                  f"logged by hand — Harvest's {cell['hours']}h skipped")
            counts["conflict"] += 1
            continue
        counts[write_row(notion, ids["time_entries_ds_id"], existing,
                         {"id": pid, "name": pname}, {"id": projid, "name": projname},
                         date, cell["hours"], description)] += 1

    zero = f", {counts['zero']} zero-hour rows dropped" if counts["zero"] else ""
    if args.dry_run:
        print(f"\nDRY RUN — nothing written. {counts['planned']} rows would be "
              f"created/updated{zero}.")
        for (pname, _, projname, _, date), cell in sorted(days.items(), key=lambda kv: kv[0])[:400]:
            print(f"  {date}  {pname:<24} {projname:<36} {cell['hours']:5.2f}h  "
                  f"{', '.join(cell['notes'])[:60]}")
    else:
        print(f"\nDone: {counts['created']} created, {counts['updated']} updated, "
              f"{counts['unchanged']} unchanged, {counts['conflict']} skipped "
              f"(hand-logged){zero}.")


if __name__ == "__main__":
    main()
