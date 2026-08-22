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

## August 2026 runs

August has been pulled in three passes, all with the same flags as July
(`--nonbillable-project "Bear Website" --allow-unassigned`). No Harvest project went
unmapped and no one was off-assignment in any of them, so those two warnings stayed
silent all month.

| Run date | Range asked for | Result |
|---|---|---|
| 2026-08-07 | 2026-08-03..08-06 | 14 rows, 15.50 h, 5 people |
| 2026-08-19 | 2026-08-03..08-19 | 31 created, 3 updated, 11 unchanged, 1 collision — 53.33 h new |
| 2026-08-22 | 2026-08-17..08-22 | 6 created, 5 unchanged — 13.50 h new |

After the third pass the Time Entries database holds **213 Harvest rows, 546.91 h**,
covering 2026-07-01 through 2026-08-21 (nothing was logged on the 22nd, a Saturday).

The 2026-08-19 pass imported 53.33 h for 6 people: Pablo Saracca 17.50 h (Neurogum,
The Human Bean, True Temper), Joaquin Kenta Heianna 12 h (Bear Website), Lautaro Ayub
11.33 h (44PRO), Zarco Nontol 6 h (five projects), Francisco Andres 5.50 h (both
Streamside projects), Juan Pablo Ghelfi 1 h (Bear Website). One collision: Melisa had
hand-logged 0.5 h on Bear Website for 2026-08-10, so Harvest's 0.5 h for that day was
skipped.

### Re-running an already-imported range is worth it

Both later passes deliberately started *before* the last import's end date, and both
found drift — people edit their Harvest timesheets after the fact:

- re-reading 08-03..08-06 (imported on the 7th) found 11 of 15 rows unchanged, 3 rows
  whose hours or notes had since changed in Harvest, and 1 row
  (Pablo / The Human Bean / 08-06, 1 h) that hadn't existed at import time at all;
- re-reading 08-17..08-22 found two more days (08-18, 08-19) that had been logged in
  Harvest *after* the 19th's pull.

So: overlap the previous run by a few days rather than starting where it stopped. The
update path is what makes that free — an unchanged row costs one comparison and no
write.

### `--dry-run` cannot tell you what will change

`--dry-run` skips the `existing_rows` read entirely, so its "N rows would be
created/updated" is just the Harvest side of the plan — it can't say which rows are new
and which already match. To preview a run *against* Notion, run it for real with the
client's writes intercepted:

```python
# reconcile.py — same code path, nothing written
import sys; sys.path.insert(0, 'src')
import sync_harvest as sh

class FakePages:
    def __init__(self, real): self.real = real
    def update(self, page_id=None, properties=None): print("UPDATE", properties)
    def create(self, **kw): print("CREATE", kw['properties'])
    def __getattr__(self, n): return getattr(self.real, n)

class FakeClient:
    def __init__(self, real): self.real = real; self.pages = FakePages(real.pages)
    def __getattr__(self, n): return getattr(self.real, n)

_real = sh.get_client
sh.get_client = lambda: FakeClient(_real())
sys.argv = ['sync_harvest.py'] + sys.argv[1:]
sh.main()
```

It prints the same `created / updated / unchanged / skipped` tally the real run does,
plus every row it would have touched.

### Pulling the data without API credentials

`.env` still has no `HARVEST_ACCOUNT_ID` / `HARVEST_TOKEN`, so all three passes fed the
script a saved payload via `--entries`, pulled through the Harvest MCP connector
(`harvest_list_time_entries`, `auto_paginate`). Two things to know about that route:

- the response is large — 17 days account-wide is ~3 MB / 1,759 entries — so it lands
  in a tool-results file rather than in the reply; copy it somewhere durable before
  using it;
- `harvest_aggregate_time` caps at 1,000 entries analyzed, so its totals are a
  truncated sample. `harvest_list_time_entries` is the only reliable source.
