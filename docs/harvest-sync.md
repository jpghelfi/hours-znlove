# Harvest → Notion sync

`src/sync_harvest.py` imports Harvest time entries into the Notion **Time Entries**
database, so hours tracked in Bear Group's Harvest show up in the hours app next to
hours logged in the app itself.

## What gets imported

Four filters, applied in this order. Everything dropped is counted and printed, so a
run always tells you what it left behind.

1. **Date range** — `--from` / `--to` (inclusive).
2. **Billable only** — non-billable Harvest time is skipped. `--include-non-billable`
   turns that off. (This is why internal work like "Bear Website (Internal)" and
   "BG: Admin" doesn't come across: it's booked as non-billable in Harvest.)
3. **znlove people only** — a Harvest user is imported only when their name matches an
   active row of the Notion **People** roster. Matching is on normalized name tokens
   (accent- and punctuation-insensitive, ≥2 tokens in common), so "Joaquin Heianna" in
   Harvest matches "Joaquin Kenta Heianna" in the roster. Everyone else in the Bear
   Group Harvest account is ignored.
4. **Assignments** — the person must be in the project's **People** property in Notion.
   Pairs that aren't assigned are reported and skipped, unless `--allow-unassigned` is
   passed (then they're imported *and* still reported).

Harvest project names carry budget/year suffixes ("SaltWorks - OSS (40h)",
"Streamside OSS - 60h (2026)"), so they're matched against Notion project names by
prefix/containment, longest name winning — `Vital Signals - OSS (80h)` beats a bare
`Vital Signals`. The few names that don't survive that rule live in `PROJECT_OVERRIDES`
at the top of the script. A Harvest project with no Notion match is reported (with its
hours) and skipped — nothing is silently dropped.

## How rows are written

Harvest entries are rolled up to **one Notion row per (person, project, day)** — the
same shape as a cell in the app's weekly grid. Hours are summed; the individual Harvest
notes are de-duplicated and joined with commas.

Every imported row's Description starts with a `Harvest` marker line:

```
Harvest
BW-30, BGONCO-232
```

That marker does double duty: it's how you tell imported time from app-logged time in
Notion, and it's what makes re-runs safe.

## Re-runs are idempotent

Before writing, the script loads the existing Notion rows in the date range and, for
each (person, project, day):

- a **Harvest-marked** row already there → updated in place if hours or notes changed,
  otherwise left alone (reported as `unchanged`);
- only **hand-logged** rows there (no marker) → the Harvest hours are **skipped** and
  the collision is printed, so app-logged time is never double-counted or overwritten;
- nothing there → a new row is created.

So the same command can be re-run any number of times, and re-running after someone
edits their Harvest timesheet pulls the correction through.

## Running it

```bash
# preview — writes nothing
./.venv/bin/python src/sync_harvest.py --from 2026-07-01 --to 2026-07-31 --dry-run

# for real
./.venv/bin/python src/sync_harvest.py --from 2026-07-01 --to 2026-07-31
```

Two ways to feed it Harvest data:

- **Harvest API** (no extra flag): set `HARVEST_ACCOUNT_ID` and `HARVEST_TOKEN` in
  `.env` — a personal access token from <https://id.getharvest.com/developers>. This is
  the path to use if the sync is ever scheduled.
- **A saved response**: `--entries harvest_july.json`, a JSON file holding the Harvest
  `/v2/time_entries` payload (an object with `items` / `time_entries`, or a bare list).
  This is how the first run was done, pulling the data through the Harvest MCP
  connector rather than storing API credentials.

## July 2026 backfill (first run)

Run on 2026-08-02 for `2026-07-01..2026-07-31`, from an MCP-pulled Harvest export:

- 3,043 Harvest entries read → **129 Notion rows, 358.00 h, 6 people**
- skipped: 1,941 entries (not on the znlove roster, 25 such Harvest users), 942 entries
  (non-billable)
- every Harvest project mapped to a Notion project — no unmapped names
- one off-assignment pair imported deliberately: **Zarco Nontol / Streamside OSS
  (14 h)** — Zarco isn't in that project's People property in Notion. Add him to the
  assignment if that's meant to be permanent; otherwise future runs need
  `--allow-unassigned` again.
- the 41 pre-existing app-logged July rows (104.5 h) were untouched, and an immediate
  re-run reported `0 created, 0 updated, 129 unchanged`.

Per person: Lautaro Ayub 121 h, Zarco Nontol 82.5 h, Pablo Saracca 82 h, Francisco
Andres 46.5 h, Juan Pablo Ghelfi 24 h, Joaquin Kenta Heianna 2 h. Melisa Bellico had
July Harvest time but all of it non-billable, so she has no imported rows.
