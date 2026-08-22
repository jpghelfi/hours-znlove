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

Editing an existing pill can change **anything about it**, the project included: the select stays live, and picking another project (or, in the Projects view, another person) rewrites the booking in place. Underneath that is still a delete plus a write — the (person, project) pair *is* the Notion row's identity — so the save carries `from_person_id`/`from_project_id` and the route clears the old pair over the same weekday range. The new booking is written first and the clear is deliberately outside its `try`: if the second half fails, the day shows both bookings, which is visible and fixable, where the other order could drop the hours entirely.

Because a typed number is a statement rather than an increment, swapping onto a project the day already books **replaces** its hours. That could quietly shrink a 6h booking to the 1h being edited, so the browser asks first, naming both numbers.

**Remove** always removes the booking that was clicked, even if the select was changed first — "Remove" on an existing pill can't mean the other one.

Saving POSTs once to `/api/allocation` and patches the affected cells, meters and totals in place. Days that the range touched but that aren't on screen (a "repeat through" reaching into next week) are simply skipped by the patcher — a reload shows them.

### Dragging a booking

Moving work is what a planner does all day — "Kepos slips to Thursday", "give
that day to Franco" — so a pill is draggable rather than open-remove-reopen-add.
The pill carries the (person, project) pair; where it lands supplies the day
and, across rows, the other half of the pair. Hold **⌥** (or Ctrl) to copy
instead of moving. Both cells, their meters and every total repaint in place.

The hours are settled by the server, not sent from the browser: a pill can be a
fold of duplicate rows for one (person, project, day) pair, and it can be stale.
Landing on a day that already books the same pair **adds** to it — you dropped
3h of Kepos onto a day already holding 2h of Kepos, and 5h is the only reading
of that gesture that doesn't silently lose hours. `ops.move_allocation` writes
the target and deletes the source under one `_write_lock`, so nothing can
interleave between the two and strand the hours.

The drop is **optimistic**: the pill is drawn in its new place on the next
frame (about a millisecond), and the Notion round-trip — a query plus one or
two writes, seconds on a cold free instance — runs behind it. Everything needed
to undo the move is captured before the DOM is touched, so a refusal or a
dropped connection puts the week back exactly as it was and says why in a
dialog; that is the only case where the wait would have been worth watching.
When the response does arrive its numbers are laid over the guess, so a
concurrent edit elsewhere can't leave a wrong total on screen. Until then the
pill wears a quiet `.is-pending` outline — settled, just not yet acknowledged.

One subtlety worth keeping: while a drag is live, the cell's own `+` and `×`
buttons get `pointer-events: none`. A drop that lands on a `<button>` is refused
by the browser and the pill snaps back — and the `+` sits exactly where an empty
cell invites you to aim, so without this the feature reads as "drag doesn't
work". The pills themselves must keep their pointer events: taking them away
mid-drag cancels the drag outright in Chromium.

Drag-and-drop is pointer-only. On touch, the popover is still the way to move a
booking.

### Clearing a cell, a day or a week

Three sizes of the same gesture, all going through one function
(`ops.clear_allocations(date_from, date_to, person_ids, project_id)`) and all
scoped to whatever the planner is filtered to, so a button only ever deletes
what is on screen:

| Control | Scope |
| --- | --- |
| **×** in a day cell (on hover) | that row's person/project, that one day |
| **🗑** under a day's date | that whole day column |
| **Clear week** | Mon–Fri |

A cell's `×` is rendered always and hidden by CSS while the cell is empty, so a
cell that fills in without a reload gets its `×` back. All three delete by page
id — the rows the read just returned — rather than re-deriving upsert keys, so a
duplicate row left by an old race goes too instead of surviving as a ghost. Each
confirms, and none of them can be undone.

### The day headers stay put

`.grid thead th` has been `position: sticky` all along, but sticky needs a
scroll container that actually scrolls: `.grid-wrap` only ever scrolled
sideways, so vertically the header left with the page and a long roster scrolled
into columns you could no longer name. The planner grid is now bounded to the
viewport and scrolls inside itself (`.plan-scroll`), which gives the header
something to stick to; the totals row sticks to the bottom edge for the same
reason, and the person column keeps its existing sticky-left.

The grid is *also* sticky itself, docked just under the top bar — otherwise the
little the page still scrolls (the hint below the grid, the footer) would slide
the whole box, sticky header and all, up behind the bar. The bar wraps to two
lines on a narrow window, so `base.html` measures it into a `--topbar-h`
variable rather than letting the CSS guess.

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

`date` must be a weekday; `through` (optional) must be on or after it and no more than 90 days out, so a fat-fingered date can't write hundreds of rows. `hours: 0` deletes. `also_assign` adds the person to the project's `People` property, keeping `/schedule` and `/assignments` consistent. Optional `from_person_id` / `from_project_id` name the pair this save replaces (an edit that switched project), cleared over the same range.

`POST /api/allocation/move` — the drag. `{person_id, project_id, date}` → `{to_person_id, to_project_id, to_date}`, plus `copy` (⌥-drag) and `also_assign`. No hours: the server reads what is actually booked on the source day.

`POST /api/allocation/clear-day` — `{date, person_ids, project_id}`. One day under the page's filters; a cell adds its own row to that scope. `clear-week` is the same call, Mon–Fri wide.

Underneath, `ops.set_allocation_range` walks the weekdays of the range and does an exact-date upsert per day (`_set_allocation_locked`), holding `_write_lock` once for the whole range so concurrent saves can't duplicate rows. Duplicate rows left over from older races are folded into one on the next write.

## Notion notes

- Allocation rows live in the Allocations data source; the date property is still called **`Week`** for historical reasons, but it now holds an exact day.
- Rows written before this rebuild sit on a Monday with the whole week's hours. They are left alone: they render as hours on that Monday until someone re-plans that week.
- **People properties come back without names** — Notion returns `{"object": "user", "id": …}`. Any name shown to a user has to be resolved against the roster (`_person_name_map`, or the `people` list a route already loaded). This also fixed the Reports forecast, which used to label every planned row "(unassigned)".
- `ops.planned_rows` still buckets allocations to their Monday, so Reports' planned-vs-actual totals are unchanged whether a week was planned as one legacy row or five day rows.
