# Monthly project budgets

An optional **monthly hour budget** per project, a policy for what happens when it runs
out, a control centre at `/budgets`, and threshold alerts. Modelled on Harvest's feature
(see `docs/budgets-plan.md` for the reference design and the decisions behind it), built
natively here — **nothing reads from or writes to the connected Harvest account.**

## The shape of it

A budget is an **allowance that resets on the 1st of every calendar month** and never
carries over in either direction. 40 h/month means 40 h every month, not a drifting
balance. Usage is keyed on the entry's own `Date`, so a backfill logged in September
against an August date spends **August's** budget — which means a month can go over
retroactively, and `/budgets` shows that rather than hiding it.

## Where it lives

On the **Projects** data source, as four properties added by `ensure_budget_properties()`
at startup (`web/notion_ops.py`) — not a database of its own, because a budget is
per-project *settings* (exactly one value per project), not dated events the way invoices
and absences are. It also means the budget rides along on the `list_projects` query the
app already runs, so a budget check costs no extra read on a hot write path.

| Property | Type | Meaning |
| --- | --- | --- |
| `Monthly budget` | number | Hours per calendar month. **Empty = not budgeted.** |
| `Budget policy` | select | `Warn only` (default) · `Block over limit` |
| `Overrun %` | number | Only read when the policy blocks. Blank = stop exactly at the budget; `10` = stop at 110 %. |
| `Warn at %` | number | Alert threshold. Blank = `BUDGET_WARN_PCT` (default **95**). |
| `Budget notified` | rich_text | `2026-08:over` — the last alert fired, so it sends once a month, not once an entry. |

**Empty is not 0.** Empty means "not budgeted"; `0` means "no hours allowed here at all",
and both are useful. (Harvest has the identical trap and documents it: a blank per-person
budget is ignored, a `0` is instantly over budget.) The only off switch is clearing the
number — an empty field, never a checkbox.

Two properties express all three behaviours that were asked for:

| Behaviour | `Budget policy` | `Overrun %` |
| --- | --- | --- |
| No limit — just track it | `Warn only` | — |
| Hard stop at the budget | `Block over limit` | blank |
| Allow up to 10 % over | `Block over limit` | `10` |
| Not budgeted at all | *(leave `Monthly budget` empty)* | — |

Every read goes through `.get()` (`_budget_from_props`). These columns are addressed by
name, and this app has been taken down by a Notion rename before (`alloc_person_prop`;
the Time Entries `Logged by` column is currently called `melisa`). A renamed budget
column reads as **"no budget"** — the project quietly stops being enforced — never a 500.

## Enforcement, and what it can't do

**Admins are never blocked.** A cap applies to non-admins only; admins can always track,
on any project, past any limit. They're the people who can change the budget, so friction
there would only teach them to route around it.

That decision removes a chokepoint rather than adding a branch:

| Route | ops call | Enforced? |
| --- | --- | --- |
| `POST /entry` (log form, incl. the timer's stop) | `create_entry(enforce=…)` | yes, for non-admins |
| `POST /api/cell` (weekly grid) | `set_cell(enforce=…)` | yes, for non-admins |
| `POST /api/entry/hours` (admin inline fix) | `set_entry_hours` | **no — already admin-only** |

The check lives in the `notion_ops` functions, not the routes: they're the ones that know
the (project, date, old hours) triple. The exemption is passed *in* as
`enforce=not auth.is_admin(user)` rather than having `notion_ops` reach for `auth` — that
would give it a first dependency on the auth module and leave the CLIs, which have no
user at all, hard to reason about. `enforce` defaults to `False`, so `src/log_hours.py`
and `src/sync_harvest.py` are untouched, which is the right behaviour: both are admin
tools, and refusing to import hours somebody genuinely worked would corrupt the record to
protect a number.

**Two rules the arithmetic depends on**, both covered by tests:

1. **`set_cell` replaces, it doesn't add.** Typing `3` into a cell that held `5` *lowers*
   the month by 2, so the check compares the **delta**, not the submitted hours — and the
   delta counts duplicate rows it's about to fold together. Checking the submitted number
   would refuse ordinary corrections all over a busy project.
2. **A write that lowers a project's month total is never refused.** Otherwise a project
   sitting over its cap could never be corrected: every edit, including the one that
   fixes it, would be "over budget".

The cap refuses the hour that *crosses* the limit, so filling a 40 h budget to exactly
40 h is allowed.

**Three write paths can't be policed**: `src/log_hours.py`, `src/sync_harvest.py`, and
Notion itself (it's the database — anyone with access can type a row in). So a hard cap
binds *this app's UI for non-admins*, not the data. **`/budgets` is the real control; the
cap is a nudge for the people not in a position to change the budget.**

## `/budgets` — the control centre

Admin-only. One calendar month at a time, reusing `_period_range`'s monthly granularity so
it can't drift from `/project`, `/invoices` and `/absences`. **One Notion read for the
whole table** — a single `entries_between` for the month grouped by `project_id` in
Python, the way `_all_projects_hours` does it. One query per project would be 37 round
trips on a free Render instance.

Six statuses, in precedence order:

| Status | Meaning |
| --- | --- |
| `Over cap` | Past the cap. Only reachable by an admin write or an unpoliced path, so it's the row most worth looking at. |
| `Over` | Past the budget, still inside the allowed overrun. |
| `At the cap` | Sitting exactly on the limit — nobody but an admin can add to it. |
| `Warning` | At or past the project's warn %. |
| `On track` | — |
| `No budget` | Unbudgeted. Still shows tracked hours: that's how you notice a project that ought to have one. |

Budgeted rows sort trouble-first; **unbudgeted rows stay alphabetical below them**. That
two-block sort is deliberate: trouble-first is right for every visit after the budgets
exist and wrong for the first one, when a list that reorders under the cursor on every
save would be unusable. The page settles itself as it fills up.

The bar is the week page's capacity meter (`cap` / `cap-full` / `cap-over`) reused rather
than a second visual language for the same idea — which also happens to match Harvest's
treatment. Each project name links into `/project` for the same month, so "why is this
over?" is one click and no new screen. `GET /budgets.csv` exports the same rows under the
same filters.

### Editing is built for the first sitting

There were no budgets to import: all ~37 get typed in by hand, once, on this page — before
any of the dashboard value exists. So:

- **Every row renders an input**, not an "＋ Add budget" affordance.
- **Enter commits and moves to the next row's budget field.** Typing a column of numbers
  never involves the mouse.
- **Saves are per field, on blur** (`POST /api/budget`) — one bad keystroke shouldn't cost
  the other 36, and there's no draft state to lose.
- **Policy defaults to `Warn only`** when a budget is first set, so entering one is a
  single keystroke sequence. Nobody picks 37 policies up front.
- A save reveals the sticky **"Budgets changed · Refresh"** bar `/project` already uses,
  since the served percentages were computed from the old numbers.

`set_budget` distinguishes "leave the number alone" (`_UNSET`) from "clear it" (`None`),
or editing a policy on its own would silently wipe the budget next to it. It writes only
the named properties — a project page's `People` is the assignment list `/schedule` and
`/assignments` depend on, and rewriting the whole property bag would clobber it — and
checks the page's parent first, since the id comes from the browser.

## The meter on the log form

When a project with a budget is picked, a line under the field shows where it stands:

> **Vital Signals** · 74.5 of 80 h in August · 5.5 h left

Served by `GET /api/budget/status`, keyed on the **entry's** date so backfilling into a
previous month reports that month. When the caller would be capped it also says how much
can still be logged, which is not the same as the hours left once an overrun is allowed.

This is the most valuable part of the feature. Harvest's whole failure mode is that budget
feedback arrives the next morning, by email, to somebody other than the person logging the
hours — this lands at the moment of the decision. It's also why the endpoint shows a
budget to a non-admin: they're a member of the project and about to be held to its cap, and
a limit you can't see is a trap.

A refused save explains itself in numbers rather than a fixed string — "40 h budget, 39 h
already logged, 1 h left". On `/entry` that message rides back through the URL (capped and
escaped); on `/api/cell` it's a **409** and the grid snaps the cell back to its previous
value, because leaving the typed number on screen would read as saved.

## Alerts

Two levels — `warn` at the project's percentage, `over` at 100 % — each firing **once per
project per month**, recorded in `Budget notified`. That stamp is Harvest's
`over_budget_notification_date` idea: without it, every subsequent entry in an over-budget
month sends another email. A new month means the stamp no longer matches, so it fires
again, which is what a monthly budget should do. Dropping from `over` back to `warn`
doesn't re-alert.

Fired **at the write that crosses**, not by a scheduler — there's no scheduler in this
repo, and firing at the crossing is more useful anyway. Deliberately indifferent to who
logged the hours: admins pass through the cap untouched, so an admin overrun is precisely
what this exists to catch.

Transport is `mailer.send_plain` over the Gmail API (**Render's free tier blocks the SMTP
ports** — see `docs/invoices.md`). A send failure is logged and swallowed: the hours are
the point, the email is a courtesy, and a Gmail outage must never stop anyone logging time.

## Config

| Env var | Default | Meaning |
| --- | --- | --- |
| `BUDGET_WARN_PCT` | `95` | Fallback threshold when a project's `Warn at %` is blank |
| `BUDGET_ALERTS_ENABLED` | off | Master switch for budget emails, separate from the Google credentials |
| `BUDGET_ALERT_TO` | falls back to `REPORT_TO` | Recipients |

The switch is separate from the credentials for the same reason `REPORT_EMAIL_ENABLED` is:
one Google authorization powers the Sheets export and the report email, and turning either
on must not silently start emailing people about budgets.

**No new database**, so no `databases.json` or Render id wiring — the one piece of setup
this feature doesn't need. The properties appear on the Projects db the first time the app
boots.

## Tests

`./.venv/bin/python tests/test_budgets.py` — 36 checks, no Notion calls, no pytest
dependency. Covers the parsing (including a renamed column and the empty-vs-0 trap), the
month bounds, the cap (crossing, overrun, reductions, a 0 h budget, floating-point
exactness), `set_cell`'s delta arithmetic including folded duplicates, the six statuses,
the two-block sort, and the alert stamp's whole state machine.

## Not built

Per-month budget overrides (so changing a budget doesn't retroactively rescore past
months — see the plan's "known flaw"), whole-project rather than monthly budgets, per-person
and per-task budgets, and any notion of money. This bills hours, not money, the same as
`/invoices`.
