# Absences — who's off, and when

`/absences` does two things on one screen: log that you're away, and read who's away. It's open to everyone, not just admins — the point of it is that people file their own.

## Logging one

The form asks for a first day, an optional last day, and a reason. It never asks **who**: an absence is always filed for the logged-in person, the same way `/api/cell` only ever writes the caller's own hours. Leave the last day blank and it's a single day off.

`POST /absences` is a plain form post that redirects back to the view it was filed from (`period` + `anchor` + any people picks ride along as hidden inputs), with `ok=` or `err=` carrying the outcome. Everything it refuses — a backwards range, a blank reason, a range longer than a year — comes back as a sentence above the form rather than a 400.

## What a row is

**One row per absence**, holding the whole stretch in a single Notion `Dates` property (start + end). Not one row per day, which is how allocations work:

- an absence is one decision with one reason — writing the reason on ten rows says the same thing ten times;
- removing it should be one click, not ten;
- a range in one date property is what Notion's calendar and timeline views want.

`Days` is stored alongside, holding the **weekday** count, so the Notion table shows what an absence costs without anyone counting on their fingers. Weekends never count — a Friday-to-Monday absence is two days, not four (`ops.weekdays_between`).

| Property | Type | Notes |
| --- | --- | --- |
| `Absence` | title | `<name> · 10 Aug – 14 Aug 2026` |
| `Person` | people | who is off |
| `Dates` | date | start, plus `end` for a range |
| `Days` | number | weekdays covered |
| `Reason` | rich text | capped at `MAX_ABSENCE_REASON` (400) |

Created by `src/setup_absences_db.py` (idempotent). On Render set `ABSENCES_DB_ID` and `ABSENCES_DS_ID`; both id paths, as always. Until then `ops.absences_enabled()` is false, the page still renders, and it says what to run.

## Reading it

### The overlap query

`ops.list_absences(from, to)` returns every absence **overlapping** the period, not only those inside it: a fortnight off that started in June is still what someone is doing on the 1st of July.

Notion's date filters compare against a range's *start*, so "ends on or after X" can't be asked for server-side. The query instead reaches back a bounded window (`_MAX_ABSENCE_DAYS`, 366) before the period and settles the far edge in Python. The bound is what keeps that read small.

### The board

One period at a time — a week or a month — with the same prev/now/next toolbar as `/project`, and `_period_range` reused wholesale.

| Period | Columns | A cell |
| --- | --- | --- |
| **Weekly** | Mon–Fri | ● if they're off |
| **Monthly** | the weeks touching the month | how many days off that week |

A month of weekday columns would be 22 of them, so the monthly view buckets into weeks — the same Days/Weeks split the schedule makes.

`_absence_days` expands each row into weekdays **clipped to the period**, keyed `person → {date: reason}`. Two consequences worth knowing: an absence straddling the period edge counts only the days inside it (while the list underneath still shows the whole thing), and two absences overlapping on one day count that day **once** — a dict of dates can't double-count.

### Scope

Admins see everyone and get the shared people filter (`_people_filter.html`, the same one `/reports` and `/schedule` use). Everyone else sees only their own, and the filter isn't drawn — the pick is dropped server-side too, not just hidden.

## Removing one

`POST /api/absence/delete` archives the page. Two checks happen inside the write lock **before** the write, because the id comes from the browser: the page's parent must be the Absences data source (the same guard `set_entry_hours` uses), and the caller must own the row — `any_person=True` is the admin's escape hatch. Otherwise anyone who could read an id could cancel someone else's holiday.

The board above the list is a server-rendered aggregate, so a successful delete reloads the page rather than leaving stale totals next to a row that's gone.

## What it deliberately doesn't do

- **No approval flow.** You log that you're off; nobody signs it.
- **No allowance or balance.** It doesn't know how many days you get.
- **No effect on the schedule or reports.** `/schedule` will still let you book someone who's away, and absent days don't appear in capacity math. Wiring the two together is the obvious next step, and is a separate decision from recording the days.
