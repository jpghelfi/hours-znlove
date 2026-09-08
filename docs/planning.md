# Planning: a project's week or month as a timeline

`/plan` (admins), `/plan.pdf`, `/p/<token>` + `/p/<token>.pdf` (no login). Analysis and
the decisions behind this shape are in `docs/planning-plan.md`; this is how it works.

## What it is

One project at a time, one period (a Mon–Fri week or a calendar month), two views over
the same rows:

- **Timeline** — a row per scheduled item, a bar across its weekday columns coloured by
  status and filled to tracked ÷ estimate, a diamond for a milestone, a red rule on
  today, and a **tray** of unscheduled items under the grid.
- **Tickets** — the same items as a list grouped by status (Backlog · Planned · In
  progress · Blocked · Done · Dropped), with an inline status select.

A **plan item** is a feature, task, bug or milestone with dates, an estimate in hours, a
status, an owner, an optional goal, an optional Notion ticket link, an internal note and an
`Internal` flag. Done and Dropped rows sink below the live ones.

**Admins only, to see and to change.** Members get no Plan link and `/plan` sends them
home, like `/budgets` and `/invoices`.

## Where it lives in Notion

Its own **Plan items** database (`src/setup_plan_db.py`; `PLAN_DB_ID` / `PLAN_DS_ID` on
Render, `databases.json` locally — both id paths, as always). `Project` relates to
Projects, `Goal` to Goals (only if the Goals db exists when the setup runs), and `Blocked
by` is a self-relation stored for a later dependency view. `Dates` is one range property,
like Absences, so Notion's own timeline view draws the plan too: empty = unscheduled,
start = end = a single day. `Estimate`: **empty ≠ 0**, the budgets/goals rule. Plain
dates only, never datetimes — an item ending Friday must not become Thursday 23:00 in
another timezone.

Not reused: **Goals** (standing buckets that feed the reports and the goal picker; a
week's plan is a dozen day-long rows that would drown them) and the **ticket boards**
other teams own (whose columns they rename). An item can *point at* a ticket — the same
`parse_task_url` the log form uses, so pasting a link needs no Notion permission and the
label fills from the slug.

Every property read goes through `.get()` (`_plan_row`), so a column renamed in the
Notion UI reads as empty rather than 500ing — the `alloc_person_prop` lesson. The share
settings are three columns on the **Projects** row, added at startup by
`ensure_plan_properties()`: `Plan share token`, `Plan share shows people`, `Plan share
shows hours`.

## Reads and writes

`list_plan_items(project_id)` is **one** query on the Project relation, everything else
in Python: the page needs the unscheduled rows as much as the ones overlapping the
period, and Notion's two-level filter nesting can't say "empty OR overlapping" in one
filter. `_plan_period_rows` in `app.py` clips each item to the weekday columns (`col_a`
/ `col_b`, 1-based inclusive, `clip_l`/`clip_r` when it runs past an edge) and drops
items entirely outside the period; a weekend-only item has no column and is dropped
too.

**Tracked hours** (`plan_tracked`) resolve per item, first match wins: a `Goal` →
`goal_totals` (lifetime, cached); a ticket → the sum of the project's entries carrying
that ticket's URL, read over **one** window covering the period and every shown item's
own dates; neither → no figure, the bar stays flat. Nothing enforces an estimate.

Writes go through `POST /api/plan/item` (create, or patch by page id — the browser
names the `fields` it means, so a status change can't blank the note),
`/api/plan/item/move` (a drag), `/api/plan/item/delete` and `/api/plan/share`. All are
admin-gated and `_same_origin`-checked, and every item id from the browser is checked
against the Plan data source before a write (`_own_plan_item`, the `get_invoice` rule).
Saves reload the page: the rows are server-rendered and a plan is small.

**Drag** on the timeline is pointer-based: a bar moves by whole weekday columns (both
real dates shift by that many weekdays, so a clipped bar keeps the days off screen), its
right-edge grip resizes the end, and a tray item is HTML5-dragged onto a day cell or a
day header. The move is optimistic — refused, it snaps back and says why. Below 720px
there is no drag, only the dialog; double-clicking an empty cell starts a new item on
that day.

## Sharing

**Share** on `/plan` mints a token (`secrets.token_urlsafe(32)`) onto the project's row.
`/p/<token>` renders the live plan for whoever has it, with no login: every open re-reads
Notion, there is no snapshot. Resolution is one Notion query with the token *in the
filter* (`Plan share token equals …`), cached ~60 s — no list of tokens is ever held —
after a length and charset check so garbage never reaches Notion. **Revoke** blanks the
token; the old link 404s within the TTL. **New link** rotates it.

**The strip** (`_plan_public_rows`, tested) decides what the link shows: internal items
are gone; notes, tickets, goals and owner ids never pass; owners and estimate/tracked
hours only when the project's two share flags are ticked. Default is the client-safe
one. The page wears `base.html` without the menu (`public=True`), sends `Cache-Control:
no-store`, `X-Robots-Tag: noindex` and `Referrer-Policy: no-referrer`, and nothing on it
links into the app. The timeline and tickets partials are shared with `/plan` and only
draw what they are handed; read-only there means no click targets and no script.

**PDF** (`web/plan_pdf.py`): landscape A4, the timeline drawn on the canvas (bars,
diamonds, today rule, weekly bands) then a page listing the items. One builder for
`/plan.pdf` (full) and `/p/<token>.pdf` (stripped); the rows decide the columns. The
timeline draws at most 40 bars and says how many more are in the list rather than
shrinking to unreadable.

## Not built (yet)

Dependencies drawn as arrows (`Blocked by` is stored), sub-items, grouping by owner, a
project "update + health" paragraph, and a capacity line against `/schedule` bookings —
all in `docs/planning-plan.md` as v1.5/v2. Baselines, critical path, sprints and comments
were rejected there on purpose.

`tests/test_plan.py` (28 checks, no Notion, no pytest).
