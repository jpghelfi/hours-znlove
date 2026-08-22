"""Harvest → Notion: matching and planning. Pure functions, no I/O.

The caller supplies the roster, the projects and the Notion rows already in the
range; this module decides what should be written. Keeping it I/O-free is what
lets the CLI (src/sync_harvest.py) and the web page (web/harvest_ops.py) share
one matcher and one set of upsert rules instead of drifting apart — the same
reason web/ imports from src/ rather than duplicating notion_ops.

Two rules run through everything here:

* **A Harvest entry is never silently misfiled.** Where the old CLI took the
  first roster hit and the longest project name, ambiguity is now returned as
  ambiguity so the screen can name it and a human can settle it.
* **Hand-logged rows are never touched.** A day someone typed into this app
  outranks whatever Harvest says about it, always.
"""
from __future__ import annotations

import collections
import unicodedata

# The Description's first line on rows this sync wrote, from before Time Entries
# had a Source property. Still read (July's rows carry it), no longer the
# discriminator — a select can be filtered in the Notion query, a string prefix
# can't, which is what makes the stale-row pass affordable.
MARKER = "Harvest"
SOURCE = "Harvest"          # the value of the Source property on synced rows
MAX_SYNC_ROWS = 500         # one burst, not a job — mirrors MAX_COPY_ROWS


def norm(s: str) -> str:
    """Accent-folded, punctuation-free, single-spaced lowercase."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in s).lower().split())


def name_tokens(s: str) -> set:
    return {w for w in norm(s).split() if len(w) > 2}


# ---- matching ----------------------------------------------------------

def match_person(entry_user: dict, roster: list) -> tuple[dict | None, str]:
    """Map a Harvest user onto a roster person. Returns (person, why).

    `why` is "id" | "name" | "unknown" | "ambiguous".

    An explicit `harvest_id` on the People row wins outright: it is the only
    key that survives someone being renamed on either side, and with a member
    token there are no emails to fall back on (/v2/users 403s).

    Name tokens stay as the fallback so nothing regresses on day one, but two
    hits are now **ambiguous**, not "the first one". This roster has both a
    Juan Pablo Ghelfi and a Pablo Saracca: a Harvest "Juan Pablo Saracca"
    shares two tokens with each, and the old code filed those hours under
    whichever sorted first, silently.
    """
    hid = entry_user.get("id")
    if hid is not None:
        explicit = [p for p in roster if p.get("harvest_id") == hid]
        if explicit:
            return explicit[0], "id"
    toks = name_tokens(entry_user.get("name", ""))
    if not toks:
        return None, "unknown"
    hits = [p for p in roster if len(toks & name_tokens(p["name"])) >= 2]
    if len(hits) == 1:
        return hits[0], "name"
    if len(hits) > 1:
        return None, "ambiguous"
    return None, "unknown"


def match_project(entry_project: dict, projects: list) -> tuple[dict | None, str]:
    """Map a Harvest project onto a Notion project. Returns (project, why).

    `why` is "id" | "name" | "unmapped" | "ambiguous".

    Explicit ids on the Notion row win, and they are a **list**: one Notion
    project legitimately covers several budget-suffixed Harvest projects
    ("Vital Signals - OSS (80h)" and "Vital Signals" are one client). That
    direction is normal and stays silent.

    The other direction is an error. Harvest names carry budget/year suffixes,
    so a Notion name still counts as a match when it is a prefix of, or
    contained in, the normalized Harvest name — but when two different Notion
    projects both claim one Harvest project, that is returned as ambiguous
    instead of resolved by "longest name wins". Our `Saltworks` and the
    ticket board's `Salworks` are one keystroke apart; guessing files a real
    client's hours against the wrong project.
    """
    hid = entry_project.get("id")
    if hid is not None:
        explicit = [p for p in projects if hid in (p.get("harvest_ids") or [])]
        if len(explicit) == 1:
            return explicit[0], "id"
        if len(explicit) > 1:
            return None, "ambiguous"
    hn = norm(entry_project.get("name", ""))
    if not hn:
        return None, "unmapped"
    exact = [p for p in projects if norm(p["name"]) == hn]
    if len(exact) == 1:
        return exact[0], "name"
    if len(exact) > 1:
        return None, "ambiguous"
    cand = [p for p in projects
            if hn.startswith(norm(p["name"]) + " ") or norm(p["name"]) in hn]
    if not cand:
        return None, "unmapped"
    if len(cand) == 1:
        return cand[0], "name"
    # several Notion projects contained in one Harvest name: only unambiguous
    # if one is the longest by itself ("Vital Signals - OSS" over "Vital
    # Signals"); a tie is a real collision and has to be settled by hand.
    longest = max(len(p["name"]) for p in cand)
    top = [p for p in cand if len(p["name"]) == longest]
    return (top[0], "name") if len(top) == 1 else (None, "ambiguous")


# ---- planning ----------------------------------------------------------

def _is_synced(row: dict) -> bool:
    """A row this sync owns: the Source property, or July's Description marker."""
    return row.get("source") == SOURCE or (row.get("description") or "").startswith(MARKER)


def plan(entries: list, roster: list, projects: list, existing: dict,
         opts: dict | None = None) -> dict:
    """Everything the sync would do, decided but not done.

    `existing` maps (person_id, project_id, date) -> [row, …] as read from
    Notion, each row {page_id, hours, description, source}.

    Returns {rows, stale, counts, unknown_people, unmapped_projects, ambiguous,
    off_assignment, totals}. `rows` carry an `action` — create / update /
    unchanged / conflict — decided here rather than at write time, so the
    preview and the apply can't disagree about what is about to happen. That is
    the one thing the CLI's --dry-run never did: it skipped the existing-rows
    read entirely, so it could not tell "3 already exist unchanged" from
    "3 new rows".
    """
    opts = opts or {}
    date_from, date_to = opts.get("date_from", ""), opts.get("date_to", "")
    include_non_billable = bool(opts.get("include_non_billable"))
    nonbillable_ok = {norm(n) for n in (opts.get("nonbillable_projects") or [])}
    allow_unassigned = bool(opts.get("allow_unassigned"))

    counts = collections.Counter()
    unknown_people: dict = {}
    unmapped_projects: dict = {}
    ambiguous: dict = {}
    off_assignment: dict = {}
    days: dict = collections.defaultdict(lambda: {"hours": 0.0, "notes": [], "ids": []})
    person_cache: dict = {}
    project_cache: dict = {}

    def bucket(store, key, label, hours):
        row = store.setdefault(key, {"label": label, "hours": 0.0, "entries": 0})
        row["hours"] = round(row["hours"] + hours, 2)
        row["entries"] += 1

    for e in entries:
        hours = e.get("hours") or 0
        date = (e.get("spent_date") or "")[:10]
        # Harvest's own date filter did this already; re-checking here means a
        # paging mistake drops rows out of the plan instead of writing them
        # onto a week nobody asked about.
        if date_from and date_to and not (date_from <= date <= date_to):
            counts["out of range"] += 1
            continue
        if e.get("is_running"):
            # a timer still running has partial hours — it will be complete on
            # the next sync, and importing half a day now is just noise
            counts["timer still running"] += 1
            continue

        huser = e.get("user") or {}
        ukey = huser.get("id") or huser.get("name")
        if ukey not in person_cache:
            person_cache[ukey] = match_person(huser, roster)
        person, why = person_cache[ukey]
        if not person:
            store = ambiguous if why == "ambiguous" else unknown_people
            bucket(store, "person:%s" % ukey, huser.get("name") or "(unnamed)", hours)
            counts["not on the roster" if why != "ambiguous" else "ambiguous person"] += 1
            continue

        hproj = e.get("project") or {}
        pkey = hproj.get("id") or hproj.get("name")
        if pkey not in project_cache:
            project_cache[pkey] = match_project(hproj, projects)
        project, pwhy = project_cache[pkey]
        if not project:
            store = ambiguous if pwhy == "ambiguous" else unmapped_projects
            bucket(store, "project:%s" % pkey, hproj.get("name") or "(unnamed)", hours)
            counts["no Notion project" if pwhy != "ambiguous" else "ambiguous project"] += 1
            if pwhy != "ambiguous":
                unmapped_projects[("project:%s" % pkey)]["harvest_id"] = hproj.get("id")
            continue

        # Billable is checked after the project is known, so a named exception
        # can let internal work through: Harvest marks *whole projects*
        # non-billable ("Bear Website (Internal)"), which is the only reason
        # that time can ever be imported at all.
        if not (e.get("billable") or include_non_billable
                or norm(project["name"]) in nonbillable_ok
                or norm(hproj.get("name", "")) in nonbillable_ok):
            counts["non-billable"] += 1
            continue

        if person["id"] not in (project.get("member_ids") or []):
            bucket(off_assignment, (person["name"], project["name"]),
                   "%s on %s" % (person["name"], project["name"]), hours)
            if not allow_unassigned:
                counts["not assigned to the project"] += 1
                continue

        cell = days[(person["id"], project["id"], date)]
        cell["hours"] += hours
        cell["person"] = person
        cell["project"] = project
        if e.get("id") is not None:
            cell["ids"].append(e["id"])
        note = (e.get("notes") or "").strip()
        if note and note not in cell["notes"]:
            cell["notes"].append(note)

    rows = []
    for (pid, projid, date), cell in sorted(
            days.items(), key=lambda kv: (kv[0][2], cell_name(kv[1]))):
        hours = round(cell["hours"], 2)
        if hours <= 0:
            # Harvest keeps 0h entries (a timer started and cleared); here a
            # zero-hour cell means "no entry", so there is nothing to write
            counts["zero-hour"] += 1
            continue
        prior = existing.get((pid, projid, date), [])
        mine = [r for r in prior if _is_synced(r)]
        row = {
            "person_id": pid, "person": cell["person"]["name"],
            "project_id": projid, "project": cell["project"]["name"],
            "date": date, "hours": hours,
            "notes": ", ".join(cell["notes"]),
            "harvest_ids": cell["ids"],
            "page_id": mine[0]["page_id"] if mine else None,
        }
        if prior and not mine:
            # someone logged this day by hand in the app — theirs wins, always
            row["action"] = "conflict"
            row["existing_hours"] = round(sum(r["hours"] for r in prior), 2)
        elif mine:
            same = abs(mine[0]["hours"] - hours) < 0.005
            row["action"] = "unchanged" if same else "update"
            row["existing_hours"] = mine[0]["hours"]
        else:
            row["action"] = "create"
        counts[row["action"]] += 1
        rows.append(row)

    # rows this sync wrote that the plan no longer covers: deleted in Harvest,
    # or dropped to zero hours. Reported always; only archived when asked.
    covered = {(r["person_id"], r["project_id"], r["date"]) for r in rows}
    stale = []
    for key, prior in existing.items():
        if key in covered:
            continue
        for r in prior:
            if _is_synced(r):
                stale.append({"page_id": r["page_id"], "person_id": key[0],
                              "project_id": key[1], "date": key[2], "hours": r["hours"]})
    counts["stale"] = len(stale)

    writes = counts["create"] + counts["update"]
    return {
        "rows": rows,
        "stale": stale,
        "counts": dict(counts),
        "unknown_people": sorted(unknown_people.values(), key=lambda v: -v["hours"]),
        "unmapped_projects": sorted(unmapped_projects.values(), key=lambda v: -v["hours"]),
        "ambiguous": sorted(ambiguous.values(), key=lambda v: -v["hours"]),
        "off_assignment": sorted(off_assignment.values(), key=lambda v: -v["hours"]),
        "totals": {
            "entries": len(entries),
            "rows": len(rows),
            "writes": writes,
            "hours": round(sum(r["hours"] for r in rows), 2),
            "people": len({r["person_id"] for r in rows}),
            "over_cap": writes > MAX_SYNC_ROWS,
        },
    }


def cell_name(cell: dict) -> str:
    """Sort key for a planned day: person, then project, both by name."""
    person = (cell.get("person") or {}).get("name", "")
    project = (cell.get("project") or {}).get("name", "")
    return person.lower() + "\x00" + project.lower()


def description(notes: str) -> str:
    """What goes in a synced row's Description.

    The MARKER prefix is kept so a row written now is still recognised by the
    July-era reader (and by anyone eyeballing the Notion table), even though
    Source is what the queries filter on.
    """
    return MARKER + ("\n" + notes if notes else "")
