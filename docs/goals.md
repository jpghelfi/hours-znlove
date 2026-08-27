# Goals

A **goal** is a named bucket of work inside a project — "New homepage",
"Maintenance" — that logged hours are filed under, so a project's month reads as
*what it went into* rather than only who spent it and when.

`docs/goals-plan.md` is the analysis this was built from: the five models that
were weighed, the data that ruled three of them out, and the flows.

## The shape of it

Assignment is **retroactive and admin-only**. Nobody's logging changes: people
log hours exactly as before, and whoever knows what the work was for files them
afterwards from `/project`. That is deliberate — half the entries in this
database have a blank description and only 17 % carry a ticket, so a grouping
that depended on the person logging would be empty, and asking eight people to
categorise their own hours from day one is how the feature would die.

Two shapes of goal, told apart by `Target basis`:

| | New homepage | Maintenance |
| --- | --- | --- |
| `Target hours` · `Target basis` | 80 · `Total` | 10 · `Per month` |
| `Due` | 2026-10-31 | *(empty — it never ends)* |
| Status when spent | `Done` | stays `Open` |
| The meter reads | 62 of 80 h, **all time** | 8 of 10 h, **this month** |

A **standing** goal is one with `Per month` and no `Due`: a single Notion row
that January's and December's entries both point at. Not a row per month — that
would be 12 × 27 rows a year and a picker nobody could use by March.

Because a standing goal's lifetime total only ever grows, **every read-out is
period-scoped**: the block on `/project` shows the period on screen, and only
the target meter reaches past it (and only for a `Total` goal, which is measured
over its life by definition).

**Nothing enforces a target.** Unlike `Monthly budget`, a goal never blocks a
save and never emails. A cap on a goal would only teach people to log against no
goal, which destroys the data the feature exists to collect. Warn, show, never
block.

## Where it lives

A database of its own (`src/setup_goals_db.py`), related from Time Entries by a
`Goal` relation added at startup by `ensure_goal_property`.

| Property | Type | Meaning |
| --- | --- | --- |
| `Goal` | title | "New homepage" |
| `Project` | relation → Projects | scopes the picker; two projects can both have a "Maintenance" without sharing a row |
| `Target hours` | number | **Empty = untargeted.** `0` would mean "no hours allowed", the same trap `Monthly budget` documents |
| `Target basis` | select | `Total` (default) · `Per month` |
| `Status` | select | `Open` · `Done` · `Dropped` — only `Open` goals are in the picker |
| `Started` / `Due` | date | a standing goal leaves `Due` empty |
| `Note` | rich_text | what "done" means |

**Not a select column on Time Entries**, which was the cheap option: a Notion
select is global to the database, so one option list would carry every goal of
all 27 active projects, and Notion **creates an option for any name it doesn't
know** — one typo forks a goal in two. (The same trap `docs/notion-task-links.md`
documents for the ticket board's Project column.) A select also holds a name and
nothing else: no target, no dates, no status.

**Not a membership list stored on the goal**, which would have made filing one
write instead of N — the trick `/invoices` uses for `Adjustments`. It inverts
every question the app asks: "which goal is this entry in?" would mean loading
every goal, the Notion query could no longer filter by goal, and someone reading
Time Entries in Notion — the source of truth — would see no goal at all. An
invoice adjustment is a decision *about* entries; a goal is a property *of* the
work.

**The Goal column is resolved by what it points at, not by its name**
(`goal_prop`): a relation knows its target data source, so renaming the column in
the Notion UI can't take a page down. That's the `alloc_person_prop` lesson —
the Allocations people column was once renamed to `val` and 500'd two pages — and
the Time Entries "Logged by" column is called `melisa` today. A missing relation
reads as "goals aren't set up", never a `KeyError`.

## Filing entries: the flow, and the cost it's built around

**Notion has no bulk update.** Filing N entries is N round trips at roughly 3
requests a second: 46 entries is ~15 seconds, 200 is over a minute — past a
request timeout on a free Render instance. Everything about the UI follows from
that:

- The browser sends the selection in **batches of `MAX_GOAL_ASSIGN` (25)** and
  reports real progress ("filing 31 of 46…") rather than one request that looks
  hung. The template gets the batch size from the same constant the endpoint
  enforces, so the two can't drift.
- A batch over the cap is **refused before the first write**, whole rather than
  half-applied — the rule `clear_week_allocations` already follows.
- One row that fails doesn't lose the other 24: `set_entry_goals` collects
  failures and returns them, and the browser leaves those rows untouched.
- **No `_write_lock`.** It's global and non-reentrant, and holding it through 25
  updates would stall every other save in the app. Goal assignment races with
  nothing — it touches one property no other write reads.
- Only the `Goal` property is written. Rewriting a page's whole property bag
  would clobber everything else on the entry.

**Validation without a retrieve per entry.** Entry ids come from the browser, so
they can't be trusted — but checking each page's parent (the rule
`set_entry_hours` follows) would double the round trips. Instead the endpoint
re-reads the entries logged for **this project and period** in one query and
refuses anything outside that set. Cheaper, and strictly stronger: it also stops
an entry being filed under another project's goal.

**Undo is one action deep.** Each entry's previous goal is captured before the
write, so undo re-files them where they were — including entries that had a
different goal, not just unfiled ones. That's what makes ticking 46 rows a
low-stakes click.

## The screens

**`/project`, one project selected** — goals only appear here, because a goal
belongs to a project. The block above the person table gives each goal its
hours, share and target meter, and a last row for **Unassigned** that is never
hidden and never sorted away: for the first months it is the biggest number on
the page, it is the backlog, and dropping it would leave the block disagreeing
with the project total directly above it. Clicking a row filters the page (and
its exports) to that goal; `?goal=none` is the triage view.

**Goals are edited from the row they're on.** The ✎ beside a goal opens the
same dialog with its name, target, basis, **due date** and status — the four
things that change after a goal exists. Setting `Status: Done` takes it out of
the picker while keeping its hours and its row (badged `Done`) for any period it
has hours in; `Dropped` is the same for work that was abandoned. A blank Due is
what a standing goal looks like, so clearing it is a first-class action rather
than an omission — hence `clear_target` / `clear_due` on the request, and
`_UNSET` versus `None` inside `update_goal`, the distinction `set_budget` makes
for exactly the same reason: editing a status must never silently wipe the
target beside it.

**The picker creates as well as picks.** `＋ New goal` opens a dialog that also
lists **names other projects already use**, because the cross-project report
groups by name — a second spelling of "Maintenance" is a second row in the one
table that exists to put them together. Names are matched with `_norm`, the same
case/punctuation-insensitive compare `match_project_option` uses. Creating a goal
with entries selected files them in the same move.

That dialog renders from `base.html`'s `modals` block, **outside the period
form** — a `required` control inside a closed `<dialog>` inside a form makes the
browser refuse the form's submit with nothing to focus to explain why. That is
exactly how "Save entry" silently stopped working the day the ticket dialog
shipped (`_task_dialog.html`).

**`/reports` → By goal** — the cross-project view, grouped by goal name for the
same period and people filter as the rest of the page. One project is named
outright; several are counted ("6 projects"). Unassigned is a row here too.

**Exports** — `/project.csv` gains a `goal` column, and the workbook gains a
**By goal** table per project sheet plus a Goal column in the log. Both only when
the project actually uses goals, so an export from a project that doesn't is
exactly the file it was before. A `?goal=` on the page rides along to the CSV,
the workbook and the export screen, so a link copied off `/project` exports what
`/project` was showing.

## Setup

```bash
./.venv/bin/python src/setup_goals_db.py     # idempotent; writes databases.json
```

On Render, set `GOALS_DS_ID` (and `GOALS_DB_ID`) to the printed values. Until
they're set, `goals_enabled()` is false and every screen behaves exactly as it
did before — no block, no tick column, no file bar, and `/api/goal` and
`/api/entry/goal` return 403. The `Goal` relation on Time Entries is added by the
app at startup once the database exists.

## Tests

`./.venv/bin/python tests/test_goals.py` — 29 checks, no Notion calls, no pytest
dependency. Covers the tolerant parsing (a half-empty page, a renamed column, the
empty-vs-0 target), the batch guard rails (the cap refused whole, the
out-of-period id refused whole, a partial failure, dashed-vs-bare ids), the two
target bases, the rule that Unassigned is always a row, and the report's
group-by-name.

## Not built

A goal picker on the log form (phase 2 of the plan — worth it once the goals
exist and are stable, not before), goal-aware planning on `/schedule`, goals on
the invoice screen, sub-goals, and any notion of money. This groups hours, not
budgets them.
