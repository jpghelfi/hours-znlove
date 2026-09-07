# Planning: a project's week or month as a timeline — analysis

**Status: analysis only. Nothing built.** Written the way `docs/goals-plan.md` and
`docs/budgets-plan.md` were — the reference design and the decisions behind it, before
any code.

## The ask

A **Planning** section: pick a project, plan its week or its month. Two views of the same
things — a **timeline** (bars on days) and a **tickets** list — over items that can be a
big feature or a small task, each with dates, an estimate and a status. Notion stays the
database. And the plan has to leave the building: as a **PDF**, or as a **link anyone can
open**, no login, that shows the timeline *as it is right now*.

That last sentence is the one that shapes the design. Everything else in this app sits
behind Notion OAuth and a roster; this is the first page that a client, a partner or a
contractor without a Notion seat will open. So the model has to know which fields are
theirs to see and which are ours.

## What the other tools do, and what is worth taking

Looked at Linear (projects, milestones, updates, public sharing), Asana (timeline),
Notion's own timeline view, Jira (roadmap/plans), monday, ClickUp/TeamGantt, Basecamp
(hill chart, lineup), Float and Productboard. What follows is what earns its place in a
tool this size — a small studio planning client work, not a PMO.

| Feature | Who does it well | Take it? | Why |
| --- | --- | --- | --- |
| Bars on a day grid, drag to move, drag the end to resize | Asana, Notion, TeamGantt | **v1** | The `/schedule` planner already has pill dragging; a bar is a pill with two ends. |
| Zero-length **milestone** (a diamond, not a bar) | Asana, Jira, TeamGantt | **v1** | "Launch 24 Sep" is the line a client actually reads. It's a `Type`, not a second table. |
| **Status** with a fixed vocabulary, colour = status | Linear, Jira | **v1** | Backlog · Planned · In progress · Blocked · Done · Dropped. Six, and no more — a status a client can read without a legend. |
| **Unscheduled** items in their own tray | Asana | **v1** | An item with no dates is still a plan; hiding it because it has no bar is how things fall off. |
| **Today** line | everyone | **v1** | The `/schedule` today-stripe, vertical. |
| **Progress** on an item and on the project | Linear, Jira | **v1** | Linear computes project progress from issue states; here it's *done items ÷ items*, and for an item with an estimate, *tracked ÷ estimate*. We already own the tracked hours — that's the one thing no roadmap tool has. |
| **Estimate vs tracked** on the bar | ClickUp, monday (baseline) | **v1** | The point of putting planning inside a time tracker. See [Tracked hours](#tracked-hours-the-thing-only-we-can-do). |
| **Public read-only link** | Linear, Asana, Productboard, monday (guest views) | **v1** | The ask. Tokened URL, no login, live. |
| **PDF export** | Asana, TeamGantt, monday | **v1** | reportlab is already a dependency (`invoice_pdf.py`). |
| Group rows by **status / owner / type** | Notion, Asana | **v1.5** | Cheap once the rows exist; owner grouping is what a stand-up wants. |
| **Project update**: a short "where we are" note + health (On track / At risk / Off track) | Linear (the feature people cite most) | **v1.5** | Two properties on the Projects row. Shows on the share page as the paragraph above the timeline — the thing a client wants before the bars. |
| **Capacity** line: hours booked on this project this week vs estimates due | Float, Teamwork | **v1.5** | `/schedule` already knows the bookings. "40 h of estimates due, 24 h booked" is a sentence no other tool can say without a second product. |
| **Dependencies** ("blocked by", arrows between bars, auto-shift) | Asana, Jira, TeamGantt | **v2** | Drawing arrows on a grid that scrolls in two axes is real work, and a 4-person shop rarely needs the auto-shift. Store the relation from day one so v2 is only drawing. |
| Sub-items / epics → stories | Jira, Linear, Notion | **v2** | Two tiers is one tier too many for a weekly plan. A `Parent` self-relation is cheap to store; nesting the grid is not. |
| Baselines (plan vs the plan you had) | monday, TeamGantt | **no** | Rewards PMO reporting, not shipping. |
| Critical path | Asana, MS Project | **no** | Needs dependencies and durations to mean anything; wrong for an agency. |
| Comments / activity on an item | Linear, Asana | **no** | The item is a Notion page — its body and comments live there, one click away. Don't build a second inbox. |
| Sprints / cycles | Linear, Jira | **no** | The period picker (week / month) *is* the sprint, without the ceremony. |

## The data model

### A · Plan items are Goals with dates ← rejected, narrowly

The Goals database (`docs/goals.md`) already carries most of this: a `Project`, `Started`,
`Due`, `Status`, `Target hours`. A feature *is* a goal with a bar. Putting Planning on top
of Goals means tracked hours roll up for free (every entry filed under the goal) and one
fewer database.

It breaks on scale and on meaning. Goals are **standing buckets** — "Maintenance", "Q3
migration", a dozen per project over a year — and every one of them shows up in the
by-goal report and the entry-filing picker on `/project`. A week's plan is fifteen
items on Fotosprint alone, most of them a day long; filing them as goals turns the goal
picker into a ticket list and the cross-project report into noise. `Per month` goals have
no end date and no place on a timeline. And `delete_goal` refuses while entries hang under
it, which is right for a goal and wrong for "we dropped that ticket".

### B · A **Plan items** database, related to Projects and (optionally) to Goals ← **recommended**

One database under the Hours Tracker page, `src/setup_plan_db.py` alongside the other
setup scripts, `PLAN_DB_ID` / `PLAN_DS_ID` on Render, both id paths as always.

| Property | Type | Notes |
| --- | --- | --- |
| `Item` | title | |
| `Project` | relation → Projects | required; the page is one project at a time |
| `Dates` | date (range) | start **and** end in one property, like Absences — so Notion's own timeline view draws it too. Empty = unscheduled. A milestone has start = end. |
| `Status` | select | Backlog · Planned · In progress · Blocked · Done · Dropped |
| `Type` | select | Feature · Task · Bug · Milestone |
| `Estimate` | number | hours. **Empty ≠ 0**, same rule as budgets and goals. |
| `Owner` | people | the roster; at most one, read like `PM` (`_role_from_props`) |
| `Goal` | relation → Goals | optional. Set it and the item's tracked hours are the goal's. |
| `Ticket URL` + `Ticket` | url + rich_text | the pair Time Entries already carries; `parse_task_url` fills the label from the slug, offline |
| `Note` | rich_text | ours; never on the share page |
| `Internal` | checkbox | ticked = kept off the share page and the PDF |
| `Order` | number | manual sort within a status/day; Notion has no row order |
| `Blocked by` | relation → Plan items (self) | stored from day one, drawn in v2 |
| `Created by` | created_by | free |

Everything is read through `.get()` — the `alloc_person_prop` lesson: a column someone
renames in Notion reads as empty rather than 500ing the page.

Why not the ticket boards themselves (the ones `task_sources()` searches)? They belong to
other teams, their schemas differ per board, the integration has *read* access to some and
*write* to one, and a client-facing timeline can't depend on a column another team may
rename on Tuesday. The plan is ours; a plan item can *point at* a ticket, and the ＋ New
ticket dialog can create one, but the plan doesn't live there.

### Tracked hours: the thing only we can do

An item shows **estimate** and **tracked**, and its bar fills to `tracked ÷ estimate`.
Tracked resolves in this order, and the first match wins:

1. `Goal` set → `goal_totals(project_id)[goal_id]` (already cached, one read per project).
2. `Ticket URL` set → the sum of the project's entries whose `Task URL` page id matches
   (`entries_between` for the period already carries `task_url`; one read for the page).
3. Neither → no tracked figure, and the bar is flat. No guessing from descriptions.

This is what makes the section worth building here instead of using Notion's timeline
view on the raw database: the tracked column is live and honest, and nothing has to be
typed twice. **Nothing enforces an estimate**, for the reason `docs/goals.md` gives — a
cap teaches people to stop estimating.

## The page

`GET /plan?project=<id>&period=week|month&start=<date>&view=timeline|tickets`

- **One project at a time.** The share link is per project, the PDF is per project, and
  a timeline across 35 projects is the `/schedule` Projects rollup, which exists. The
  project pick is the shared `_project_filter.html` in single mode; no pick lands on the
  first active project.
- **One period**, `_period_range` reused (week or month — the Mon–Fri week or the whole
  calendar month, with the `←` · label · `→` trio from `docs/navigation.md`). Columns are
  days. A month is 30 narrow columns, which is fine on a desktop and is why phones get the
  tickets view by default. Items that *overlap* the period are shown; a bar is clipped at
  the edges with a chevron, so a three-week feature is visible in every week it touches.
- **Timeline view**: rows = items (sorted by start, then `Order`, then title), grouped by
  status with Done sunk to the bottom; a bar per item coloured by status, filled by
  tracked ÷ estimate; a diamond for a milestone; the today line; an **Unscheduled** tray
  under the grid (drag a tray item onto a day to schedule it for that day). Drag a bar to
  move it, drag its right edge to resize, click it to open the item popover (title, dates,
  status, type, estimate, owner, ticket, goal, note, internal). Same optimistic-drop
  pattern as `/schedule`: the bar lands on the next frame, the write runs behind it, a
  refusal snaps it back and says why.
- **Tickets view**: the same items as a list grouped by status, with an inline status
  select on every row — the view for Monday morning and for a phone. Not a
  kanban board: six columns don't fit a phone and drag-between-columns is the select with
  more steps.
- **Header**: project name, the PM/AM chips `/project` already draws, the progress bar
  (done ÷ items), estimate total vs tracked total for the period, and the three buttons —
  **Share** (admins), **PDF**, **＋ Item**.
- **Nav**: a flat **Plan** link after Schedule on desktop (it's an everyday page); on the
  phone, in the More sheet. `nav_plan` block, `nav` entry, sheet link — the three places
  `docs/navigation.md` names.

### Who may do what

| | read | change anything | share |
| --- | --- | --- | --- |
| member | — | — | — |
| admin | all | all | all |
| link holder | the shared project, non-internal items | — | — |

**Admins only, to see and to change** (JP, 2026-09-07). Members don't get a Plan link
and `/plan` 303s them home, as `/budgets` and `/invoices` do. A plan is a promise made
to a client; it belongs with the pages that make promises. If the team later wants a
board of their own, the tickets view is the piece to open up — status changes on their
own projects — and nothing in the model has to move.

Write endpoints, all admin-gated, all `_same_origin`-checked:
`POST /api/plan/item` (create/update by page id), `POST /api/plan/item/status`,
`POST /api/plan/item/move` (dates, from a drag), `POST /api/plan/item/delete`,
`POST /api/plan/share` (mint / revoke the token). Every item id comes from the browser, so
every write retrieves the page and refuses one whose parent isn't the Plan data source —
the `set_entry_hours` / `get_invoice` rule.

## Sharing

### The link

`GET /p/<token>` and `GET /p/<token>.pdf`, **no login**, outside `_require_login` — the
only two routes besides `/login`, `/healthz` and static files that are.

- The token is minted on **Share**, 32 bytes of `secrets.token_urlsafe`, and stored on the
  **Projects row** in a `Plan share token` rich_text property added by
  `ensure_plan_properties()` at startup (beside `Plan health` / `Plan update` for the
  v1.5 project update). One token per project; **Revoke** blanks it and the old link 404s
  within the cache TTL. **Regenerate** is revoke + mint.
- Resolution is one Notion query, `Plan share token equals <token>`, cached ~60 s like
  `access_ids`. Nothing is derived from the project id, so a link can't be guessed from
  another link, and comparing happens inside Notion's filter, not in Python — there is no
  list of tokens to compare against in constant time because we never hold one.
- The page renders from the same `_plan_rows()` the logged-in page uses, then **strips**:
  no `Internal` items, no notes, no owners by default, no estimate/tracked hours by
  default, no ticket links (they'd 404 for the reader anyway — and leak board names). Two
  checkboxes on the Share dialog, stored beside the token as `Plan share shows: people,
  hours`, let an admin turn owners and hours on for a partner who should see them. The
  default is the client-safe one.
- `Cache-Control: no-store`, `X-Robots-Tag: noindex`, no `<meta>` referrer leaking the
  token onward, and the page's own nav is the project name and nothing else — no link into
  the app. The layout is `base.html`'s shell without the menu (a `public` flag), so it
  looks like the app without offering it.
- **Live** means live: every open re-reads Notion. No snapshotting, no "published on"
  copy — the ask was that the link *is* the timeline.

Read-only there means what it means on `/schedule` for a non-admin: no click targets, no
popover JS, bars are `<div>`s not `<button>`s.

### The PDF

`web/plan_pdf.py`, next to `invoice_pdf.py`, and like it a module that turns rows already
read into bytes. Landscape A4: a title block (project, period, generated date, the
project update if there is one), the timeline drawn with reportlab's canvas — a day
column per weekday, bars as filled rectangles coloured by status, milestones as diamonds,
the today line — then a table of the items (name, type, status, dates, and, when the
viewer is logged in, estimate/tracked). The share page's PDF uses the stripped rows, the
app's PDF the full ones; one function builds both.

Month on one page: 22 weekday columns at ~11 mm each fits landscape A4 with a 60 mm name
column. A week gets wider columns, not more of them.

## Pitfalls worth naming now

1. **Notion has no row order.** `Order` is a number the drag writes; new items get
   `max + 10`. Sorting is start-date first, so `Order` only settles ties — nobody has to
   maintain it.
2. **A date range in Notion is inclusive and timezone-less.** `Dates` is stored as plain
   dates (`YYYY-MM-DD`), never datetimes — an item ending "Friday" must not become
   Thursday 23:00 for someone in Buenos Aires. Absences already made this choice.
3. **The share page runs as the app, not as the reader.** Same fact `docs/notion-task-links.md`
   records for search: the token gates the *route*, the strip gates the *fields*. A bug in
   the strip is a leak, so the strip is one function with a test, not a set of `{% if %}`s
   in the template.
4. **Drag on a month grid.** A 30-column grid at 1200 px is 40 px a day — fine for a
   drop, tight for grabbing a bar's edge. The resize handle is 12 px and drawn outside the
   bar; below 720 px there is no drag at all, only the popover.
5. **Free-tier Render is slow to Notion.** The page is two reads (items for the period,
   entries for the period for the tracked figures) plus the cached goal totals — never a
   read per item. The share page is the same two reads, plus the token resolution.
6. **Reportlab and a 40-item month.** Rows overflow one page; the table continues on page
   two, the timeline does not — it draws the first 40 rows and says "and N more" rather
   than shrinking to unreadable. Same trade `invoice_pdf` makes.

## Build order

| Step | What | Size |
| --- | --- | --- |
| 1 | `setup_plan_db.py`, id wiring, `ensure_plan_properties()`, `list_plan_items` / `save_plan_item` / `set_plan_item_dates` / `delete_plan_item` in `notion_ops.py`, `plan_enabled()` degrade | S |
| 2 | `/plan` tickets view + item popover + status endpoint — the whole feature is usable from here, on any screen | M |
| 3 | Timeline view: grid, bars, milestones, today, unscheduled tray, drag move/resize (the `/schedule` drag lifted, not copied) | L |
| 4 | Tracked hours on the bars, header totals, project progress | S |
| 5 | Share: token mint/revoke, `/p/<token>`, the strip and its test, public layout | M |
| 6 | `plan_pdf.py`, both routes | M |
| 7 | v1.5: group-by, project update + health, capacity line | M |

Steps 1–2 ship first as their own PR; a tickets list with status is already the tool the
team lacks today, and the timeline lands on top of it.

## Decisions for JP

1. **One project at a time on `/plan`** — or should the timeline also roll up a partner
   (Bear's four projects on one grid)? Recommended: one project in v1, partner rollup
   read-only in v1.5 the way `/schedule` does it.
2. ~~Members can change status~~ — **decided: admins only, to see and to change.**
3. **Share defaults**: people and hours **off** unless the admin ticks them per project.
   Recommended as written.
4. **Ticket boards stay pointed-at, not planned-in.** Recommended as written; the
   alternative (reading the ticket boards as the plan) is a different, more fragile product.
5. **Nav**: flat "Plan" after Schedule — or under Reports ▾? Recommended flat.
