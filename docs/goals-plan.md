# Grouping logged hours into goals — analysis

**Status: analysis only. Nothing built.** Written the way `docs/budgets-plan.md` was —
the reference design and the decisions behind it, before any code.

## The ask

An admin should be able to take a bunch of hours logged against a project and file them
under a named goal — "New homepage", "Maintenance", "Q3 migration" — so the project's
hours read as *what they went into*, not just *who spent them and when*.

Two verbs hide in that sentence, and they want different things:

1. **Group** — retroactively sweep existing entries into a bucket. This is the one the
   ask names outright ("group a bunch of logged hours").
2. **Target** — "set a particular goal" also reads as *a number to hit*: 80 h for the
   homepage. That is a budget with a different shape (see [Targets](#targets) below).

Build them in that order. The grouping is useful with no target attached; a target with
nothing grouped under it is inert.

## What the data says

Measured against live Notion, not assumed:

| | July 2026 | August 2026 |
| --- | --- | --- |
| entries | 348 | 346 |
| projects with hours | 28 | 27 |
| entries carrying a **ticket** | 0 | 61 (**17 %**) |
| entries with a **blank description** | 144 (41 %) | 168 (**49 %**) |
| busiest single project-month | 116 entries | 64 entries (Fotosprint) |

Three conclusions fall straight out:

- **The grouping key has to be an explicit field.** Half the entries have no description
  and 83 % have no ticket, so nothing already on an entry can be mined into goals.
- **Whatever it is must be near-free at log time.** People already skip the one optional
  free-text field they have. A goal picker that costs a decision on every save will be
  skipped exactly as thoroughly, and the feature will look broken.
- **Retroactive assignment is the primary flow, not a migration afterthought.** Every
  entry ever logged (~350/month, so ~4 000 by now) starts life goal-less, and will keep
  arriving goal-less from the weekly grid (see [pitfall 3](#3-the-weekly-grid-cant-set-a-goal)).
  A goal spanning a quarter on a busy project is **150–200 entries** to sweep.

## Five ways to model it

### A · A `select` property on Time Entries

One column, one dropdown. Cheapest possible change.

Fails on the shape of the data: a Notion `select` is **global to the database**, so one
option list would carry every goal of all 27 active projects — "Maintenance" would be one
shared option across all of them, and picking a goal means scrolling ~80 options to find
the three that belong to your project. Notion also **creates a select option for any name
it doesn't recognise** (the trap already documented for the ticket board's Project column
in `docs/notion-task-links.md`), so one typo silently forks a goal in two. And a select
holds a name and nothing else — no target hours, no dates, no status, no notes.

### B · A Goals database, related from Time Entries ← **recommended**

A `Goals` db parented to the same Hours Tracker page, each row related to one project, and
a `Goal` relation on Time Entries pointing at it.

| Property | Type | Why |
| --- | --- | --- |
| `Goal` | title | "New homepage" |
| `Project` | relation → Projects | scopes the picker; two projects can both have "Maintenance" without sharing a row |
| `Target hours` | number | empty = untargeted, the same empty-is-not-0 rule as `Monthly budget` |
| `Status` | select | `Open` · `Done` · `Dropped` — an open-goals-only picker is what keeps it short |
| `Started` / `Due` | date | the goal's own lifetime, which is what the read-out needs (see [pitfall 2](#2-the-period-model-fights-a-goals-lifetime)) |
| `Note` | rich_text | what "done" means |

This is the same call the codebase already made twice, for the same reason: budgets are
*settings* so they live as properties **on Projects**, while invoices and absences are
*dated things with their own fields* so they got databases. A goal is the second kind — it
has a name, a target, a lifetime and a status of its own, and there are many per project.

It also buys the two things a select can't: **Notion-side filtering** (`relation contains`
in the query, the way `project_entries` already filters on Project — so a goal view is one
read, not a scan) and a real page per goal that a human can open in Notion.

### C · Derive goals from the ticket already on the entry

Entries can carry a Notion ticket (`Task URL` / `Task`). Group the tickets, and the hours
follow.

**The data kills this**: 17 % coverage in August, 0 % in July. It would also make goals
hostage to boards another team owns and renames.

Worth keeping as a *convenience* later — "file every entry on ticket X under this goal" is
a good bulk-select filter — but not as the model.

### D · A goal is a saved filter, not a stored field

Store no membership at all. A goal is a rule — description matches, ticket in this set,
these people, this date range — evaluated at read time.

Genuinely attractive: zero migration, zero writes, retroactive by construction, and a goal
can be re-scoped by editing one rule. But with half the descriptions blank there is
nothing to match on, "did this entry make it in?" becomes unanswerable without running the
rule, and two overlapping rules silently double-count the same hour. It's a reporting
convenience built on data that doesn't exist here.

### E · Membership stored on the goal, as a list of entry ids

One write per assignment instead of N — the trick `/invoices` already uses for
`Adjustments` (a JSON blob chunked across rich-text objects, cap 100 × 1 900 chars ≈ 5 700
ids, so the capacity is genuinely there).

But it inverts every question the app asks. "Which goal is this entry in?" means loading
every goal and searching; the Notion query can no longer filter by goal; and someone
reading Time Entries in Notion — which is the source of truth — sees no goal at all. The
invoice precedent works because an adjustment is *a decision about* entries, deliberately
not written onto them. A goal is a property *of* the work.

### Side by side

| | A · select | **B · Goals db** | C · from tickets | D · saved filter | E · list on goal |
| --- | --- | --- | --- | --- | --- |
| Per-project scoping | ✗ global list | ✓ | ✓ | ✓ | ✓ |
| Works on today's data | ✓ | ✓ | ✗ 17 % | ✗ 49 % blank | ✓ |
| Holds a target / dates / status | ✗ | ✓ | ✗ | ✗ | ✓ |
| Filterable in the Notion query | ✓ | ✓ | ~ | ✗ | ✗ |
| Visible in Notion on the entry | ✓ | ✓ | ✓ | ✗ | ✗ |
| Cost to assign 200 entries | 200 writes | 200 writes | 200 writes | 0 | **1 write** |
| Worst failure | typo forks a goal | dangling relation | boards renamed | silent double-count | grouping invisible in Notion |

**B.** E's single write is the one real advantage anything has over it, and it's an
optimisation for a flow that runs a handful of times per goal — paid for with the
grouping being invisible in the database that is supposed to be the source of truth.

## Where it would live in this app

No new nav tab. The top bar is at **nine items and already wraps to three rows on a
phone** — goals belong inside `/project`, which is already the "where did this project's
hours go" screen and is already admin-only.

| Piece | Where | Notes |
| --- | --- | --- |
| Assign entries | `/project`, the entry list at the bottom | it already renders every entry of the period with its id; add a checkbox column and a "File under…" action |
| Goal breakdown | `/project`, above the person table | one row per goal: hours, share, % of target, plus **Unassigned** |
| Drill into a goal | `/project?goal=<id>` | same page, filtered — reuses `_period_range`, `_project_picks` and the export chain |
| Manage goals | a small section on `/project` for the selected project | create / rename / close; a full CRUD page is not worth a tab |
| Pick at log time | the log form, under Project | **phase 2** — default to the project's single open goal, blank when there are several |
| In exports | `/project.csv`, `.xlsx`, the export screen | a Goal column, and a per-goal subtotal block in the workbook — this is likely the real client-facing payoff |

## Targets

Only after the grouping works. A goal target is **not** the monthly budget with a
different number: `Monthly budget` resets on the 1st and never carries over, while "80 h
for the homepage" is a fixed total spanning months. `docs/budgets.md` lists exactly this
under **Not built** ("whole-project rather than monthly budgets").

What *is* reusable is everything around the number: `_budget_from_props`'s empty-vs-0
rule, the six-status precedence, the `cap` / `cap-full` / `cap-over` meter, and the
alert-stamp state machine. Reuse the presentation, write a separate accumulator — and
resist enforcement. A cap that blocks logging against a goal will only teach people to
log against no goal, which destroys the data the feature exists to collect. **Warn, show,
never block.**

## Pitfalls, in the order they'll bite

### 1 · Bulk assignment is N writes, and there is no batch API

200 entries is 200 `pages.update` calls. Notion's limit is ~3 requests/second, so a
quarter's sweep on a busy project is **over a minute** — far past a request timeout on a
free Render instance, which also cold-starts.

The existing guard rails to copy: `MAX_COPY_ROWS` (500) refuses an over-large write
**before** the first one lands, so a too-big operation is refused whole rather than half
applied. Add to that: assign in the background with a progress read-out, or cap a single
sweep and say what was left. And **do not take `_write_lock`** — it's global and
non-reentrant, so holding it for 70 s stalls every save in the app. Goal assignment
doesn't race with hours arithmetic; it needs no lock.

Idempotency matters too: re-running a partially-failed sweep must be safe, which
relation-set-to-X is naturally (unlike an append).

### 2 · The period model fights a goal's lifetime

Every hours screen is *one* period — `_period_range` returns one month, one week or one
day, deliberately ("not a window of several"). A goal spans months. Showing the homepage
goal inside August's page answers a question nobody asked.

So a goal view needs its **own** range, derived from the goal's `Started`/`Due` or from
its first and last entry, and shown as such — not the period picker with a filter on top.
That's the one place this feature genuinely breaks the page's existing model, and it's
worth being explicit about rather than bolting a `?goal=` onto the month.

### 3 · The weekly grid can't set a goal

`/api/cell` upserts on (person, project, date) with no room for a goal in the UI, and
`set_cell` writes only `Hours` and `Person` on an existing row. Two consequences:

- Good: an existing entry's Goal **survives** a grid edit. Keep it that way — widening
  that update to a full property bag would wipe goals the way rewriting a project's
  property bag would wipe its `People`.
- Bad: every entry the grid *creates* is goal-less, forever, unless swept later.

Options: a per-project **default goal** applied at creation (a real cost — silently
attributing hours to a goal nobody chose), or accept that the `/project` sweep is the
assignment flow. Recommend the latter, plus making the **Unassigned** row loud enough that
the backlog is visible.

### 4 · Notion renames, again

`alloc_person_prop` exists because someone renamed the Allocations people column to `val`
and took down two pages with a `KeyError`; the Time Entries `Logged by` column is
*currently* called `melisa`. Resolve the `Goal` property from the data source schema, and
make a missing one read as "no goals configured" — never a 500. Same for the Goals db
itself: `goals_enabled()`, degrading quietly, exactly like `invoices_enabled()` and
`absences_enabled()`.

### 5 · One goal per entry, not many

Use a `single_property` relation. A many-to-many makes every total ambiguous — an hour in
two goals is either double-counted or split by a rule nobody can see, and the goal
breakdown stops summing to the project total. One goal per hour keeps the arithmetic
honest and matches how the work actually happened.

### 6 · "Unassigned" is a first-class row

For months, most hours will have no goal. If the breakdown quietly omits them, its total
disagrees with the number at the top of the same page. Show `Unassigned` as a row, with
its hours, and let it be the entry point to the sweep.

### 7 · A deleted goal leaves dangling relations

Archiving a goal in Notion leaves its id on every entry, and a relation to an archived
page reads back as an id with no name. Resolve names through a cached map with a fallback
label, the way `_project_name_map` already handles a missing project ("(none)"), and
prefer `Status = Dropped` over deletion.

### 8 · Naming

Harvest calls this concept a **Task**, and this codebase already uses "Task" for the
Notion ticket on an entry. Keep the user's word — **Goal** — and don't let the two blur.

## Suggested phasing

| Phase | What | Rough size |
| --- | --- | --- |
| **1** | `src/setup_goals_db.py`, `ensure_goal_property`, `list_goals`/`set_entry_goal`, checkbox-select + "File under…" on `/project`'s entry list, a goal breakdown table with Unassigned | 1–2 days |
| **2** | Goal column in CSV/XLSX + per-goal subtotals in the workbook, `?goal=` drill-in with the goal's own date range, goal picker on the log form | ~1 day |
| **3** | `Target hours` + the meter and statuses (warn only, never block), goal on the export/invoice screen | ~1 day |
| later | goal-aware planning on `/schedule`, "file every entry on ticket X" as a bulk filter | — |

Phase 1 alone answers "where did the homepage hours go". Everything after it is leverage
on data that only exists once phase 1 has been used for a month.

## What not to do

- **Not a `select`** — one global option list across 27 projects, and Notion invents an
  option for every typo.
- **Don't infer goals from descriptions or tickets** — 49 % blank, 17 % ticketed. An
  inferred grouping that's wrong is worse than no grouping, because it looks authoritative.
- **Don't enforce a goal target** the way `Monthly budget` enforces. Blocking a save
  against a goal teaches people to log against no goal.
- **Don't make the goal required** on the log form for the same reason.
- **Don't put it in the nav.** Nine tabs already wrap to three rows on a phone.
