# Schedule — the day-first resource planner

`/schedule` (admins only) is where you plan **who works on what, on which day, for how many hours**. It replaces the older week-first grid, where a row was a person × project pair and you typed a number into a week cell.

## Why it changed

The old grid could not express the thing it was most needed for: several projects on the same day for one person. Worse, the two granularities fought each other — a week-scope write deliberately replaced a pair's whole week with a single Monday-dated row, so editing a week cell destroyed any day-by-day plan underneath it.

The rebuild follows the shape used by Float, Resource Guru and Runn: rows are people, columns are days, and each allocation is a **pill** inside a day cell. Weekends are never shown, because nobody is scheduled on them.

## How it works

### Two views, one read

| View | What it is | Editable |
| --- | --- | --- |
| **Days** (default) | Mon–Fri of one week. One row per person (or per project), pills stacked inside each day cell. | yes |
| **Weeks** | Six Mondays, each cell the sum of that week's days. | no — click a week header to plan its days |

Both are built from a single flat read, `ops.alloc_rows(date_from, date_to, person_id)`, which returns one record per allocation (`person_id, person_name, project_id, project_name, date, hours`). `_schedule_rows` in `web/app.py` then groups those records: by date for the planner, by the week's Monday (via `ops.monday_of`) for the rollup. Adding a third granularity means adding a bucket function, not another Notion query path.

Every roster person gets a row even with nothing booked — an empty row is what you click to make the first assignment. The old "add a person/project pair first, then type hours into it" step is gone, and with it `_schedule_placeholder_rows`.

### Grouping

`?by=person` (People) puts people in rows and projects in pills. `?by=project` (Projects) flips it: projects in rows, people in pills. The same allocations, the same writes — only the pivot differs. The capacity meter is drawn for person rows only, since a project has no 8h/day ceiling.

### Who gets a row

Normally everyone on the roster gets a row, booked or not: an empty row is what
you click to make the first assignment.

A view narrowed to **one project** is the exception. "The Fotosprint week" means
the people on Fotosprint — a full company roster with four filled rows is noise,
so `_schedule_rows` prunes the rows that totalled nothing. The Projects view does
the same when the people filter narrows it. An explicit pick is never pruned:
naming people is itself a request to see them, blank week or not.

Pruning costs the one thing empty rows were for, so the page says what it hid —
"10 people with nothing booked on this project this week are hidden" — and links
back to the unfiltered planner, where the missing person has a row to click.
Totals are unaffected either way: a pruned row contributed nothing.

### Who can open it

Everyone. Only an admin can *plan*.

A non-admin gets the same page pinned to **their own row**: the route sets the
people pick to their own Notion user id and ignores any `?person=` they send, so
a hand-typed id shows their week, not someone else's. The `by=project` grouping
still works — it becomes "the projects I'm on this week", empty ones pruned by
the same rule above.

Read-only means the affordances are gone, not merely disabled: no click target
on a day cell, no `+`, pills render as `<span>` instead of `<button>`, no
Copy/Clear week, no assign dialog, and none of the popover JavaScript (which
also keeps the roster and every project's membership list out of a page that has
no use for them). The three write endpoints — `/api/allocation`,
`/api/allocation/copy-week`, `/api/allocation/clear-week` — already refused
non-admins before this and still do; the UI gating is what stops the page from
offering a click that could only 403.

One person asked for is the one filter pushed **into the Notion query**
(`alloc_rows`), rather than paged through and dropped in Python like a
multi-person pick. It's every non-admin's view of this page, so it's the read
that got the extra traffic when the page opened up.

### Assigning

Every day cell an admin can plan ends in a dashed **`+`** button, sitting in the
flow underneath the pills. It used to be an absolutely-positioned `<span>` at
`bottom: 4px`, with a 20px bottom padding on `.plan-cell` reserving room for it —
except `.grid td`'s `padding: 10px 12px` outranks a bare `.plan-cell` selector,
so the reserve never applied and the `+` landed on top of the last pill as soon
as a day held two bookings, covering its bottom 12px. In flow it cannot collide
whatever the cell holds; the padding is now written as `.plan td.plan-cell` so it
actually wins. The button is a real `<button>` with its own handler (and
`event.stopPropagation()`, since the cell behind it opens the same popover) — so
it is focusable, and visible at 50% opacity rather than only on hover, which is
the only way it exists at all on touch.

Clicking a day cell (or an existing pill) opens the assign popover:

- **Project** (or **Person**, in the Projects view) — the row pins down one side of the pair, the select offers the other. Options are split into *Assigned* (already on the project's `People` property) and *Other*.
- **Hours a day** — defaults to the day's remaining capacity, with an "Xh free of 8" hint.
- **Repeat through** — a date. Every weekday from the clicked day through that date gets the same booking, in one request.
- **Remove** — clears the pair over the same range.

When editing an existing pill the select is disabled: changing who or what means removing the booking and adding the other one, not silently rewriting it.

Saving POSTs once to `/api/allocation` and patches the affected cells, meters and totals in place. Days that the range touched but that aren't on screen (a "repeat through" reaching into next week) are simply skipped by the patcher — a reload shows them.

### Capacity

`DAY_TARGET_HOURS` (default `8`) is the per-day capacity. Each person's day cell shows a meter: under capacity is green, exactly at capacity blue, over capacity red. `WEEK_TARGET_HOURS` still overrides the number used by the weeks rollup, so existing deploys keep their weekly target.

### Colors

Each project gets one of eight palette slots from a hash of its id (`_swatch` in `web/app.py`, `--s0…--s7` in `style.css`, with light and dark values). A project therefore keeps the same color across days, across both groupings, and across page loads — no state to store.

## Writes

`POST /api/allocation` — admin-only, same-origin, body:

```json
{"person_id": "...", "project_id": "...", "date": "2026-08-05",
 "through": "2026-08-07", "hours": 4, "also_assign": true}
```

`date` must be a weekday; `through` (optional) must be on or after it and no more than 90 days out, so a fat-fingered date can't write hundreds of rows. `hours: 0` deletes. `also_assign` adds the person to the project's `People` property, keeping `/schedule` and `/assignments` consistent.

Underneath, `ops.set_allocation_range` walks the weekdays of the range and does an exact-date upsert per day (`_set_allocation_locked`), holding `_write_lock` once for the whole range so concurrent saves can't duplicate rows. Duplicate rows left over from older races are folded into one on the next write.

## Notion notes

- Allocation rows live in the Allocations data source; the date property is still called **`Week`** for historical reasons, but it now holds an exact day.
- Rows written before this rebuild sit on a Monday with the whole week's hours. They are left alone: they render as hours on that Monday until someone re-plans that week.
- **People properties come back without names** — Notion returns `{"object": "user", "id": …}`. Any name shown to a user has to be resolved against the roster (`_person_name_map`, or the `people` list a route already loaded). This also fixed the Reports forecast, which used to label every planned row "(unassigned)".
- `ops.planned_rows` still buckets allocations to their Monday, so Reports' planned-vs-actual totals are unchanged whether a week was planned as one legacy row or five day rows.
