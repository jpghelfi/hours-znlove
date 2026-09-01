# Harvest → Notion sync

`src/sync_harvest.py` imports Harvest time entries into the Notion **Time Entries**
database, so hours tracked in Bear Group's Harvest show up in the hours app next to
hours logged in the app itself.

## What gets imported

Four filters, applied in this order. Everything dropped is counted and printed, so a
run always tells you what it left behind.

1. **Date range** — `--from` / `--to` (inclusive).
2. **Billable only** — non-billable Harvest time is skipped. Two escape hatches:
   `--include-non-billable` (everything) and `--nonbillable-project NAME` (repeatable,
   matches either the Notion or the Harvest project name).

   This matters more than it sounds: Harvest flags *whole projects* non-billable, so
   internal work like **Bear Website (Internal)** — every one of its July entries,
   every person — is invisible to a billable-only run. If people expect their internal
   hours in the app, that project needs to be named explicitly:
   `--nonbillable-project "Bear Website"`. The check runs after project matching, which
   is what lets it work on the Notion-side name.
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
notes are de-duplicated and joined with commas. A day that sums to 0 h is dropped:
Harvest keeps 0-hour entries (a timer started and cleared), but in this app a zero-hour
cell means "no entry".

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

# for real, the way July was run
./.venv/bin/python src/sync_harvest.py --from 2026-07-01 --to 2026-07-31 \
    --nonbillable-project "Bear Website" --allow-unassigned
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

Run on 2026-08-02 for `2026-07-01..2026-07-31` from an MCP-pulled Harvest export, with
`--nonbillable-project "Bear Website" --allow-unassigned`:

- 3,043 Harvest entries read → **162 Notion rows, 463.91 h, 7 people**
- skipped: 2,845 entries by the 26 Harvest users who aren't on the znlove roster
- `BG: Admin 2026` (1 h) has no Notion project, so it was reported and left out
- one off-assignment pair imported deliberately: **Zarco Nontol / Streamside OSS
  (14 h)** — he isn't in that project's People property. Add him to the assignment if
  that's permanent, otherwise future runs need `--allow-unassigned` again
- one collision: Joaquin had already logged 3 h on Bear Website for 2026-07-21 in the
  app, so Harvest's 3 h for that day was skipped rather than double-counted
- one 0-hour Harvest entry (Lautaro, 2026-07-07) dropped
- the 41 pre-existing app-logged July rows (104.5 h) were untouched

| Person | Imported | Projects |
|---|---|---|
| Lautaro Ayub | 121.00 h | 44PRO |
| Zarco Nontol | 84.50 h | Vital Signals ×2, Saltworks, Streamside OSS, Bear Website, Neurogum, Centerline |
| Pablo Saracca | 82.00 h | Neurogum, Vital Signals ×2, True Temper, The Human Bean, Saltworks |
| Joaquin Kenta Heianna | 76.91 h | Bear Website, True Citrus |
| Francisco Andres | 46.50 h | Streamside OSS, Streamside 7 Parks |
| Juan Pablo Ghelfi | 31.00 h | True Temper, Neurogum, Bear Website |
| Melisa Bellico | 22.00 h | Bear Website |

Numbers that look low are usually the roster filter, not a bug: Centerline shows 1 h
because only Zarco (1 h) is znlove — the other 21.76 h that month were logged by Bear
people who aren't on the roster. Saltworks is the same story (22.5 h ours, 17.5 h
theirs).

## August 2026 backfill (second run)

Run on 2026-09-01 for `2026-08-01..2026-08-31` from an MCP-pulled Harvest export, with
the same flags as July (`--nonbillable-project "Bear Website" --allow-unassigned`).
August had **already been synced part-way during the month** (51 Harvest-marked rows,
Aug 3–21), so this was the first real test of the idempotency rules — and they held:

- 3,085 Harvest entries read → **68 day rows, 134.08 h, 7 people**
- **10 created, 1 updated, 50 unchanged, 7 skipped (hand-logged)** — the 50 unchanged
  are exactly the mid-month pull, re-verified rather than re-written
- an immediate re-run wrote nothing at all (0 created, 0 updated, 61 unchanged),
  confirming a full-month command is safe to repeat
- 3,003 entries skipped: the 21 Harvest users who aren't on the znlove roster
- **no unmapped Harvest projects and no off-assignment pairs this month** — unlike July,
  neither escape hatch was actually needed
- the 7 collisions (24.75 h) are days the person had already logged in the app at
  essentially the same hours, so nothing is missing: 134.08 h planned − 24.75 h
  already-hand-logged = the 109.33 h now carrying the Harvest marker

August in Notion after the run: 363 rows / 809.33 h — 61 Harvest rows (109.33 h) plus
the 302 rows (700 h) logged in the app.

| Person | Imported | Projects |
|---|---|---|
| Pablo Saracca | 35.50 h | Neurogum, Streamside OSS, True Temper, The Human Bean, Saltworks |
| Joaquin Kenta Heianna | 25.00 h | Bear Website |
| Zarco Nontol | 21.00 h | Vital Signals - OSS (80h), Saltworks, True Temper, Neurogum, Centerline, Bear Website |
| Francisco Andres | 11.50 h | Streamside OSS, Streamside 7 Additional parks |
| Lautaro Ayub | 11.33 h | 44PRO - Shopify Replatform |
| Melisa Bellico | 4.00 h | Bear Website |
| Juan Pablo Ghelfi | 1.00 h | Bear Website |

### Pulling the export through the MCP connector

Worth writing down, because two obvious routes are dead ends:

- `harvest_list_users` and `harvest_resolve_entities` both **403** on this account, so
  there is no way to look up the znlove people's Harvest user ids directly.
- `harvest_aggregate_time` *does* work and returns user ids, so a `group_by: ["user"]`
  call over a **week at a time** (it caps out around 1,000 entries) is the cheap way to
  discover which roster names appear in Harvest that month, and their ids.
- `harvest_list_time_entries` is the only bulk read. A full month is ~3,000 entries and
  far too large to read; the practical move is to request `per_page: 2000` for each page
  (August was 2 pages), let the harness spill each oversized response to a file, and
  merge those files into the `--entries` JSON without ever reading them. Per-user calls
  work too and are much smaller, but you need the ids from the aggregate step first.
