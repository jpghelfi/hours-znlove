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

---

# The admin's view — how this actually works

Everything below assumes the recommended model (a Goals db related from Time Entries).
Four things were asked for; each gets a flow.

## The model, adjusted for reuse

One property is added to the sketch above, because "reuse it month after month" is a
different shape from "80 h for the homepage":

| Property | Type | Meaning |
| --- | --- | --- |
| `Target hours` | number | empty = untargeted (the `Monthly budget` empty-is-not-0 rule) |
| **`Target basis`** | select | **`Total`** (default) · **`Per month`** |

That single select is the whole of "reuse":

| | New homepage | Maintenance |
| --- | --- | --- |
| `Target hours` | 80 | 10 (or empty) |
| `Target basis` | `Total` | `Per month` |
| `Due` | 2026-10-31 | *(empty — it never ends)* |
| Status when spent | `Done` | stays `Open` forever |
| What the meter shows | 62 of 80 h · 78 % | 8 of 10 h **in August** |

A **standing** goal is simply one with `Per month` and no `Due`. It is one Notion row that
entries from January and December both point at — not a new row each month. That matters:
a goal-per-month would mean 12 rows × 27 projects a year, and the picker would be unusable
by March.

**The consequence to design around:** a standing goal's lifetime total only ever grows. So
every goal read-out is **period-scoped by default** — "Maintenance, 8 h in August" — with
the all-time total available on the goal itself. A page that showed "Maintenance: 340 h"
next to August's numbers would be noise.

## Flow 1 · Creating a goal on the go

No separate "manage goals" screen to visit first. Goals are created **from the place you
needed one**, exactly the way `＋ New ticket` already works next to the ticket search on
the log form.

```
/project · Fotosprint · August 2026

  6 entries selected · 14.5 h                     [ File under… v ]
                                                  +-------------------------+
                                                  | (search) maint          |
                                                  +-------------------------+
                                                  | Maintenance      * Open |  <- this project's
                                                  |-------------------------|
                                                  | + Create "maint"        |
                                                  | Maintenance             |  <- used on 6 other
                                                  |   used on 6 projects    |     projects
                                                  +-------------------------+
```

Three things in that one dropdown, in order:

1. **This project's open goals**, filtered as you type. Closed ones are hidden unless you
   ask — the picker stays short.
2. **`＋ Create "<what you typed>"`** — one keystroke sequence from typing to filed. The
   goal is created against the current project with `Status: Open` and no target;
   everything else is editable later. Nobody sets a target while triaging entries.
3. **Names already used on other projects.** "Maintenance" will exist ~27 times, once per
   project, and the cross-project report groups them by name — so a typo forks the rollup
   in two. Offering the existing spelling is what keeps that from happening (matched with
   `_norm`, the same case/punctuation-insensitive compare `match_project_option` already
   uses). Picking one **copies the name onto a new goal for this project**; it does not
   share a row, because targets and status are per project.

One thing the codebase already learned the hard way applies here: **the create dialog must
render outside the entry form** (`base.html`'s `modals` block). A `required` field inside a
closed `<dialog>` inside a form silently blocks every submit — that's what broke logging
hours the day `TICKET_CREATE_DS_ID` was set.

## Flow 2 · Filing many entries at once

This is the flow that has to be genuinely fast, because it's the one that runs every month.

```
/project · Fotosprint · August 2026 · Hours per person

  +----------------------------------------------------------------------+
  | GOALS                       HOURS   SHARE   TARGET                    |
  | > Maintenance                 8      12%    8 of 10 h/mo  ########..  |
  | > New homepage               62      88%    62 of 80 h    #######...  |
  | > Unassigned                 46       -          <- click to triage   |
  |   Total                     116                                       |
  +----------------------------------------------------------------------+

  [x] 46 entries in this period · unassigned only v      [ File under… v ]
  +----------------------------------------------------------------------+
  | [x]  2026-08-21  Franco    4    Cambio de boton en productos (ARG)    |
  | [x]  2026-08-20  Franco    4    Incidencia canje cupon - chile        |
  | [ ]  2026-08-19  Matias    1.5  Capitalize P in Policy   · Maintenance|
  | [x]  2026-08-18  Franco    2    Cambio de boton en productos (ARG)    |
  +----------------------------------------------------------------------+
```

What makes it fast, in the order it matters:

- **Start from Unassigned.** Clicking that row filters the list to exactly the entries that
  need a decision. Triage is a shrinking pile, not a re-scan of 64 rows.
- **Select all, then deselect.** The header checkbox takes the whole *filtered* set — "all
  46 unassigned", not "the 20 on screen". Most sweeps are "all of these except two".
- **Shift-click for a range**, since entries are date-ordered and goals tend to be
  date-contiguous.
- **Filter before selecting**: by person, by text in the description, by ticket. "Every
  entry on ticket *Cookie consent solution*" is one click, and it's the one place the
  17 %-covered ticket data is genuinely useful — as a *selector*, never as the grouping.
- **The action bar states the stakes**: "6 entries · 14.5 h → Maintenance". Hours, not just
  a row count, because hours are what moves in the report.
- **Undo, for one action.** Assignment is idempotent and reversible (each entry's previous
  goal is known), so the bar keeps "↩ Undo — 6 entries back to Unassigned" until the next
  action. This is what makes selecting 46 rows a low-stakes click.

**The unavoidable cost, stated plainly:** filing 46 entries is 46 Notion writes at ~3/s —
**about 15 seconds**, and 200 entries is over a minute. So the bar shows real progress
("filed 31 of 46…"), the rows tick over as they land, and a failure part-way names exactly
what was and wasn't filed. It must not hold the global `_write_lock`; goal assignment races
with nothing.

Anything over a cap (`MAX_GOAL_ASSIGN`, ~300, mirroring `MAX_COPY_ROWS`) is refused
**before the first write**, whole rather than half-applied — the rule `clear_week` already
follows.

## Flow 3 · The report, by goal

Two places, answering two different questions.

**`/project`** answers *"where did this project's month go?"* — the block at the top of the
mockup above. Per goal: hours, share of the project, and the target meter (`8 of 10 h/mo`
for a standing goal, `62 of 80 h` for a one-off). Clicking a goal filters the whole page to
it.

**`/reports`** answers *"what is the team spending on, across everything?"* — a **By goal**
breakdown beside the existing by-project and by-person ones, for the same period and the
same people filter:

```
BY GOAL · August 2026 · everyone

  Maintenance          6 projects    64 h   #######...   18%
  New homepage         Fotosprint    62 h   #######...   18%
  Bug triage           4 projects    31 h   ###.......    9%
  Unassigned          19 projects   189 h   ############ 55%
```

Two deliberate decisions in that table:

- **Rows group by goal *name* across projects**, which is what makes a standing goal like
  Maintenance worth having — "what does maintenance cost us company-wide" is a real
  question, and it's unanswerable if every project's Maintenance is a separate row. The
  per-project split is one click down. (This is why the create picker offers existing
  names: the rollup keys on `_norm(name)`.)
- **Unassigned is a row, at its real size.** In month one it will be the biggest row on the
  page. That's the point — it's the backlog meter, and hiding it would make every other
  percentage a lie.

Both feed the exports: a `Goal` column in `/project.csv` and `/project.xlsx`, and a
per-goal subtotal block in the workbook — which is likely the real client-facing payoff,
since "62 h homepage, 8 h maintenance" is a far better line item than 46 dated rows.

## Flow 4 · A month in the life

**End of August, Fotosprint.** Open `/project`, pick the project, monthly period. The goals
block says `Unassigned 46 h`. Click it; the list filters to those 46. Select all. Most were
the homepage push — deselect the four maintenance ones. `File under… → New homepage`.
Fifteen seconds, and the block reads `New homepage 62 · Maintenance 8 · Unassigned 4`.
Select the last four, type `maint`, pick the existing **Maintenance** — done.

**Two months later**, the homepage ships. Open the goal, set `Status: Done`. It leaves the
picker, keeps its hours, and its 142 h total is on the goal. Maintenance is untouched and
still collecting — that's the reuse.

**Any month**, `/reports → By goal` shows Maintenance across all 27 projects for the month
without anybody having created a single new goal row.

## What this asks of admins, honestly

- **~1 minute per project per month** to triage, if done monthly. Done quarterly it's the
  same clicks over three times the rows and a slower write.
- **Consistent naming**, which the picker is designed to enforce rather than trust.
- **Nothing from anyone else.** Non-admins log hours exactly as they do today; the goal is
  applied afterwards by the people who know what the work was for. That's the phase-1
  design on purpose — a goal picker on the log form (phase 2) only pays off once the goals
  exist and are stable, and asking 8 people to categorise their own hours from day one is
  how this feature would die.

## Revised phasing, with the admin flows attached

| Phase | What the admin gets | Rough size |
| --- | --- | --- |
| **1** | Goals db + relation · create-on-the-go picker · select-many + File under… + Undo · goals block on `/project` with Unassigned | 2 days |
| **2** | **By goal** on `/reports` (cross-project, grouped by name) · Goal column + per-goal subtotals in CSV/XLSX | ~1 day |
| **3** | `Target hours` + `Target basis` and the meters · goal status lifecycle (Done/Dropped) | ~1 day |
| later | goal picker on the log form · goal-aware planning on `/schedule` | — |

Phase 1 and 2 together are the whole ask. Phase 3 is what makes a goal a *goal* rather
than a label, and it's safe to defer precisely because nothing enforces it.
