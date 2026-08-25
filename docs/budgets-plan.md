# Monthly project budgets — implementation plan

Status: **plan only**, nothing built yet. Written 2026-08-24.

Adds three things to the hours app:

1. **A monthly hour budget per project** — optional, so most projects can carry none.
2. **A per-project policy** for what happens when the budget runs out — warn, cap hard,
   or cap with a tolerance (e.g. 10 % over).
3. **A control centre** (`/budgets`) showing every project's tracked-vs-budget position
   for one month, plus threshold notifications on the way up.

Modelled on Harvest's feature, built natively in this app. **Nothing here reads from or
writes to the connected Harvest account** — `src/sync_harvest.py` is a separate,
unrelated importer and stays untouched.

---

## 1. What Harvest actually does (the reference design)

Taken from Harvest's real Project payload and their help docs, so these are the exact
field names, UI labels and semantics being copied — not a paraphrase:

| Harvest field | UI label | Meaning |
| --- | --- | --- |
| `budget` | — | The number (hours, or money when the budget is cost-based) |
| `budget_by` | the **Budget** dropdown | `project` = *"Total project hours"* · `task` = *"Hours per task"* · `person` = *"Hours per person"* · `project_cost` = *"Total project fees"* · `task_fees` = *"Fees per task"* · `none` = *"No budget"* |
| `budget_is_monthly` | *"Budget resets every month"* | **The budget resets on the 1st of each calendar month** rather than running for the project's life |
| `notify_when_over_budget` + `over_budget_notification_percentage` | *"Send email alerts if project exceeds X% of budget"* | **One** threshold per project, email only. Default 80 % |
| `over_budget_notification_date` | — | Read-only; last date an alert fired — **the dedup stamp**, so it doesn't nag daily |
| `show_budget_to_all` | *"Show project report to everyone on the project"* | Whether non-admins can see the budget |
| `cost_budget`, `cost_budget_include_expenses`, `fee`, `is_fixed_fee`, `hourly_rate` | — | The money side |

Per-person and per-task budget *amounts* don't live on the project at all — they're a
`budget` field on each User Assignment / Task Assignment sub-resource. And Harvest ships
a purpose-built read for the dashboard: `GET /v2/reports/project_budget` returns
`budget`, **`budget_spent`, `budget_remaining`**, `budget_is_monthly`, `budget_by` per
project — which is exactly the column set §4 lands on, arrived at independently.

**One footgun worth copying the fix for:** in Harvest a per-person/per-task budget left
**blank** doesn't count against the budget, but one entered as **0** is instantly over
budget. Same trap exists here, which is why §2 insists empty ≠ 0.

Two things worth taking from that list, and one worth deliberately *not* taking:

- **Take `budget_is_monthly`.** That is exactly the "# of hours per month per project"
  being asked for. Harvest's monthly budget is a *recurring allowance*: each calendar
  month starts fresh at zero, and **an under- or over-run does not roll into the next
  month**. Keep that rule — it's what makes the number legible ("40 h/month" means
  40 h every month, not a drifting balance).
- **Take `over_budget_notification_percentage` + `over_budget_notification_date`.**
  A threshold plus a fired-stamp is the whole notification design, and the stamp is the
  part people forget.
- **Do not take the money model.** No `cost_budget`, no `fee`, no `hourly_rate`. This
  app already decided it bills hours, not money (see `docs/invoices.md`), and budgets
  should not be the thing that smuggles rates in.

### The gap this plan closes

**Harvest has no hard stop, and no warning at entry time either.** There is no field on
the Project object for "prevent tracking past the budget" and no such setting in its UI.
Harvest's own FAQ says it outright:

> *"No, Harvest doesn't have a way to automatically prevent your teammates from tracking
> time to a project that's met its budget."*

`notify_when_over_budget` is the entire enforcement story: an email the **morning after**
the threshold is crossed, repeating weekly while over (monthly for monthly budgets).
Nothing appears while someone is actually typing hours. Harvest's documented workarounds
are all blunt — **archive the project** (blocks new time, stays reportable), tell the team
to switch to non-billable tasks, or turn on *"Show project report to everyone"* and hope
people self-monitor. Reviewers consistently describe Harvest budgets as visibility-only.

That is precisely the gap JP asked to close:

> *"proejct maye have dif logics.. that dont allow more hours tracked.. or allow until
> certin limie (eg 10%) or no limit"*

So **two** pieces of this build have no prior art in Harvest to copy: the blocking policy
(§5) and the live meter on the log-hours form (§5, end). The meter is the cheaper of the
two and probably the more valuable — Harvest's whole failure mode is that the feedback
arrives a day late, by email, to someone who isn't the person logging the hours.

Note also: this app's `Active` checkbox on a project is already the rough equivalent of
Harvest's archive workaround. It's the only "stop logging to this" lever that exists
today, and it's all-or-nothing.

---

## 2. Where the budget lives

**On the Projects data source, as new properties. No new database.**

`list_projects` already reads `Name` / `Active` / `People` in one loop
(`web/notion_ops.py:221`); budgets are per-project *configuration*, and config that
has exactly one value per project belongs on the project row. The Invoices db exists
because an invoice is an *event* with a date; a budget is a *setting*.

The live Projects schema today is only `Name` (title), `Active` (checkbox), `Client`
(rich_text), `People` (people). Four properties get added:

| Property | Type | Meaning |
| --- | --- | --- |
| `Monthly budget` | number | Hours per calendar month. **Empty = no budget** — that's how "some may have, some doesn't" is expressed. Never write 0 to mean "none"; 0 means "no hours allowed". |
| `Budget policy` | select | `Warn only` (default) · `Block over limit` |
| `Overrun %` | number | Only read when policy is `Block over limit`. Blank/0 = block exactly at the budget. `10` = block at 110 %. |
| `Warn at %` | number | Notification threshold. Blank = `BUDGET_WARN_PCT` env default (**95**). |

Two properties, not three modes, expresses all three requested logics:

| Requested behaviour | `Budget policy` | `Overrun %` |
| --- | --- | --- |
| No limit — just track it | `Warn only` | — |
| Hard stop at the budget | `Block over limit` | blank |
| Allow up to 10 % over | `Block over limit` | `10` |
| No budget at all | *(leave `Monthly budget` empty)* | — |

Added by an `ensure_budget_properties()` in the established style
(`ensure_person_property` at `notion_ops.py:30`, `ensure_task_properties` at `:41`),
registered in `_startup` (`app.py:97`) — one `data_sources.update` batching whatever
is missing, with the select options seeded:

```python
def ensure_budget_properties() -> None:
    ds = _notion.data_sources.retrieve(PROJECTS_DS)
    have = ds["properties"]
    add = {}
    if "Monthly budget" not in have: add["Monthly budget"] = {"number": {}}
    if "Budget policy" not in have:
        add["Budget policy"] = {"select": {"options": [
            {"name": "Warn only"}, {"name": "Block over limit"}]}}
    if "Overrun %" not in have: add["Overrun %"] = {"number": {}}
    if "Warn at %" not in have: add["Warn at %"] = {"number": {}}
    if add:
        _notion.data_sources.update(PROJECTS_DS, properties=add)
```

`list_projects` grows a `budget` sub-dict on each project (`{hours, policy, overrun_pct,
warn_pct}` or `None`), read in the same loop it already runs — **no extra Notion round
trip**, which matters because this dict is about to be needed on every hours write.

### Read by *schema*, not by name — the `alloc_person_prop` lesson

`web/notion_ops.py:449` resolves the Allocations people column from the data source
schema because someone renamed it in the Notion UI and took down two pages with a
`KeyError`. **That is not hypothetical here.** While mapping the live schema for this
plan I found the Time Entries created-by property — documented everywhere as
`Logged by` — is currently named **`melisa`** in Notion. Someone renamed it. Every
budget property read must use `props.get("Monthly budget", {}).get("number")`-style
access that degrades to "no budget" rather than raising, and the control centre should
say *"budget column missing — was it renamed?"* rather than 500.

### The known flaw: changing a budget rewrites history

One number per project means **raising a budget from 40 h to 60 h in September makes every
past month recompute at 60 h**. August, which really did blow a 40 h budget, silently
reads as comfortable — and since §4 sorts over-budget projects to the top, it quietly
disappears from the list.

This is inherited from Harvest, which has the identical problem and admits it:

> *"There's currently no way to change a monthly recurring budget without affecting all
> historical data."*

Harvest's fix is to **duplicate the project** whenever the budget changes, which is worse
than the disease — it fragments every report. Don't copy that.

Accept it for phases 0–1 and be honest on screen: the control centre is *"where are we
this month"*, and the current month is always computed against the current budget, which
is correct. Past months are advisory. Two cheap mitigations if it bites:

- **Show it, don't hide it.** When viewing a past month, label the budget column
  *"current budget"* rather than implying it's what was in force at the time.
- **Snapshot on invoice.** `save_invoice` already stores `hours_tracked` for a month;
  storing the budget alongside it costs one property and gives a real historical record
  at exactly the moment someone cared enough to bill it.

The full fix is the per-month override db below, and this is the reason to build it —
not the "March is 60 h" scenario.

### Deliberately deferred

- **Per-month overrides** (March is 60 h, every other month is 40 h — and, per the flaw
  above, a durable record of what each past month's budget actually was). One number
  applies to every month for now, exactly as Harvest's `budget_is_monthly` does. When
  needed, it's a `Budgets` db keyed on (project, month) copying the Invoices upsert
  pattern (`src/setup_invoices_db.py`, `save_invoice` at `notion_ops.py:1476`), read as
  an override layered over the project default — the same shape as invoice `Adjustments`.
- **Total (non-monthly) project budgets** — Harvest's `budget_is_monthly = false`. A
  `Budget period` select (`Monthly` / `Whole project`) added later; the control centre's
  math is the same, only the date window changes.
- **Per-person and per-task budgets** — Harvest's `budget_by: person` / `task`. This app
  has no task concept, and per-person capacity is already `/schedule`'s job.
- **`show_budget_to_all`** — budgets are admin-only in phase 1 (§4). Revisit only if
  people ask to see their own project's remaining hours.

---

## 3. The math, and the two ways to get it wrong

A project's **month usage** = the sum of `Hours` on Time Entries whose `Project` relation
is that project and whose **`Date` falls in the calendar month**.

**Every hour counts.** Harvest's "Total project hours" counts billable *and* non-billable
time, with a billable-only mode that's a global preference rather than per-project. This
app has no billable flag on an entry at all, so the question doesn't arise — but it's
worth knowing that's a deliberate match rather than an oversight, and that adding a
billable flag later would immediately raise "does it count against the budget?".

> Keyed on the entry's `Date`, not on when it was created. A backfill logged in September
> against an August date spends **August's** budget. This is the only reading consistent
> with the rest of the app (`/reports`, `/project`, invoices all key on `Date`), and it
> means a month can go over budget retroactively — which the control centre must show
> rather than hide.

### Trap 1 — `set_cell` replaces, it does not add

`ops.set_cell` (`notion_ops.py:887`) upserts on (person, project, date): typing `3` into
a cell that held `5` *lowers* the month by 2. A naive "would this exceed?" check that
adds the submitted hours to the current month total will be wrong on every grid edit and
every admin inline fix. The check is always:

```
projected = month_total - hours_being_replaced + hours_submitted
```

- `POST /entry` — a new row, so `hours_being_replaced = 0`.
- `POST /api/cell` — the existing (person, project, date) cell's hours, which `set_cell`
  already looks up before writing.
- `POST /api/entry/hours` — the entry's current hours. `set_entry_hours` (`:746`)
  already retrieves the page (and its `Project` relation and `Date`) at `:862` for the
  parent check, so the project, the month and the old value are all in hand with **no
  extra read**.

### Trap 2 — never block a write that reduces the total

If a project is already 12 h over a hard cap, every edit is "over budget" — including the
edit that fixes it. Deleting the bad entry would be refused and the project would be
permanently stuck.

**Rule: if `projected <= month_total`, allow it unconditionally.** A hard cap only ever
refuses writes that *increase* the month's hours. This applies to zeroing an entry
(which archives it) too.

### Where the limit sits

```
limit = budget_hours * (1 + overrun_pct/100)      # Overrun % blank → limit = budget_hours
blocked = policy == "Block over limit" and projected > limit and projected > month_total
```

Note `projected > limit`, not `>=`: filling a 40 h budget to exactly 40 h is allowed.
The cap refuses the hour that *crosses* it.

### Cost of the check

One read per write, on a path that currently does one or two:

- Single project → `ops.project_entries(project_id, month_from, month_to)`
  (`notion_ops.py:315`), which filters the Project relation **in the Notion query**.
- The budget config itself comes from a TTL-cached `list_projects`, following
  `access_ids` (`:185`) and `alloc_person_prop` (`:449`) — the two cache patterns
  already in this file. ~60 s, so a budget edited in Notion takes up to a minute to bite.

**Skip the read entirely when the project has no budget** — which is most of them. The
cached config answers that without touching Notion, so the common path costs nothing.

---

## 4. The control centre — `GET /budgets`

Admin-only, in the `{% if is_admin %}` nav group in `base.html:26-31`, following
`invoices_page` (`app.py:1155`) for the auth liturgy.

**One month at a time**, reusing `_period_range` (`app.py:607`) with the monthly
granularity and its prev/next/now anchors and month picker — the same period control as
`/project`, `/invoices` and `/absences`, so it can't drift from them.

**One Notion read for the whole page**: a single `entries_between(month_from, month_to)`
grouped by `project_id` in Python, exactly as `_all_projects_hours` (`app.py:693`) does.
Never one query per project — 37 active projects would be 37 round trips on a free
Render instance.

### The table

| Project | Budget | Tracked | Left | Used | Policy | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Vital Signals | 80 h | 74.5 h | 5.5 h | ▓▓▓▓▓▓▓▓▓░ 93 % | Block +10 % | ⚠ Warning |
| SaltWorks | 40 h | 46 h | −6 h | ▓▓▓▓▓▓▓▓▓▓ 115 % | Warn only | ● Over |
| Fotosprint | — | 22 h | — | — | — | No budget |

- **The bar already exists.** `cap` / `cap-full` / `cap-over` in `schedule.html:127`
  and `cap_pct` (`app.py:407`) are the tracked-vs-target bar from the week page — reuse
  the classes rather than inventing a second visual language for the same idea. This also
  happens to match Harvest's own treatment (blue burn bar, red once over), so the page
  will read as familiar to anyone who's used it.
- **Columns**: `budget` / `budget_spent` / `budget_remaining` is exactly what Harvest's
  own `GET /v2/reports/project_budget` returns for its dashboard — worth noting only
  because it's independent confirmation that this is the right column set, not a guess.
- **Status**, in precedence order: `Over` (≥ 100 %) → `Blocked` (at the cap, so no more
  can be logged) → `Warning` (≥ warn %) → `On track` → `No budget`.
- **Sort**: over-budget first, then by percentage descending, then name. The point of the
  page is the projects in trouble; they go at the top without being filtered for.
- **Projects with no budget still appear**, greyed, showing tracked hours. They're how
  you notice a project that *should* have a budget — and each row's Budget cell is the
  place to give it one.
- Reuse `_project_filter.html` and `_project_picks` (`app.py:772`) so the page can be
  narrowed the same way `/project` is. A totals row across the visible rows.
- Each project name links to `/project?project=<id>&period=monthly&start=<month>` — the
  existing drill-down, so "why is this over?" is one click and no new screen.

### Editing — `POST /api/budget`

Budget, policy, overrun % and warn % are editable inline on each row (admin, same-origin,
`_same_origin` at `app.py:137`), following `POST /api/entry/hours` (`app.py:1917`): save
the row, don't reload the page, but **reveal the sticky "totals are stale · Refresh" bar**
that `/project` already uses, since the served percentages are now computed from an old
budget.

Writing it is a `pages.update` on the project page. Note `set_project_member`
(`notion_ops.py:537`) is currently the *only* write to a project page — this is the
second, so it takes `_write_lock` (`:732`) like everything else, and must not clobber
`People` by writing the whole property bag.

Clearing the Budget field writes `None` (Notion clears the number) → the project drops
back to "no budget", and the policy stops applying. **This is the only "off switch"**,
and it should be obvious in the UI: an empty field, not a checkbox.

### `GET /budgets.csv`

Same rows, same filters, following `/reports.csv` and `/project.csv`. Cheap, and it's
what gets pasted into a client conversation.

---

## 5. Enforcement — and being honest about what it can't do

The three web chokepoints where hours are written:

| Route | Handler | ops call |
| --- | --- | --- |
| `POST /entry` (log-hours form, incl. the timer's stop) | `submit_entry` `app.py:227` | `ops.create_entry` `:352` |
| `POST /api/cell` (weekly grid) | `api_cell` `app.py:2135` | `ops.set_cell` `:783` |
| `POST /api/entry/hours` (admin inline fix) | `api_entry_hours` `app.py:1917` | `ops.set_entry_hours` `:746` |

The timer is client-only (`form.html:45`, `localStorage`) and submits through
`POST /entry` — no separate chokepoint.

**Put the check inside the three `notion_ops` functions, not in the routes.** They're the
functions that already know the (project, date, old hours) triple, they already hold
`_write_lock`, and a check in the route can't see the value `set_cell` is about to
replace. A `BudgetExceeded` exception carrying `{project, month, budget, limit, tracked,
attempted}` lets each route render it in its own idiom: an inline form error on `/entry`,
a rejected cell that snaps back to its old value on `/api/cell`, a 409 with a message on
`/api/entry/hours`.

### Three write paths that cannot be policed

1. `src/log_hours.py` — writes via `notion.pages.create` directly (`:50`), bypassing
   `notion_ops` entirely.
2. `src/sync_harvest.py` — same (`write_row`, `:213`).
3. **Notion itself.** Notion is the database; anyone with access can type a row into the
   Time Entries table or use a Notion form.

So a "hard cap" is a cap on *this app's UI*, not on the data. That's not a flaw to fix,
it's the architecture — but it must be said plainly in the UI, or someone will trust it
as an accounting control. Two mitigations, both cheap:

- Route the two CLIs through `ops.set_cell` / `ops.create_entry` so they inherit the
  check, with an explicit `--over-budget` flag to bypass it. (`sync_harvest` should
  probably always bypass and *report* overruns instead — refusing to import time that
  was genuinely worked is the wrong answer.)
- The control centre is the backstop: it reads the truth from Notion, so a project that
  went over by a route the app can't see still shows up red. **The dashboard is the real
  control, the cap is a convenience.**

### The warning nobody reads is the one at the moment of typing

Before any of the blocking: when a project with a budget is picked on the log-hours form,
show a live line under the project field —

> **Vital Signals** · 74.5 of 80 h this month · 5.5 h left

served by a small `GET /api/budget/status?project=<id>&date=<yyyy-mm-dd>` (HTMX, the
pattern `_task_picker.html` already uses). Keyed on the **entry's date**, so backfilling
into a previous month shows *that* month's position. This is worth more than the email
and more than the block, because it lands when the decision is being made — and it's the
piece to build first once the data exists.

---

## 6. Notifications

Harvest's model, kept: a per-project percentage threshold, and a **date stamp of the last
alert** so it fires once, not on every save.

Two of Harvest's choices deliberately *not* kept. Its alert lands the **morning after**
the crossing (~3am) and then repeats weekly while over — a batch job, because Harvest has
one. This plan fires **at the write that crosses**, which is both more useful and cheaper
here, since there's no scheduler to build. And Harvest's recipients are **derived from
permissions** rather than configured, which is why nobody can quite predict who gets one;
a plain configured list is better at this size.

- `Warn at %` on the project (default `BUDGET_WARN_PCT`, **95** — Harvest defaults to 80;
  95 fits a monthly allowance better, and it's per-project anyway).
- A fifth Projects property, `Budget notified` (rich_text), holding the month + level
  already announced, e.g. `2026-08:warn` / `2026-08:over`. Compared before sending;
  rewritten after. New month → empty → fires again. This mirrors Harvest's
  `over_budget_notification_date` and is what stops a nag on every single entry.

**Trigger: at the write chokepoints.** After a successful write, if the project crossed
its warn threshold or crossed 100 % *on this write*, and that level hasn't been announced
for this month, send. There is **no scheduler in this repo** — the only recurring pulse is
the launchd keepalive hitting `/healthz` every 10 min (`app.py:104`).

**Transport: `web/mailer.py`**, but it needs a sibling. `send_report` (`:140`) requires an
xlsx attachment; a budget alert is body-only. Add `send_plain(to, subject, body)` reusing
`build_message` (`:127`) and the Gmail-API transport (`transport()` at `:48` — **Render's
free tier blocks SMTP ports**, per `docs/invoices.md`, so this must go over the Gmail API
on 443, not SMTP).

**Its own switch: `BUDGET_ALERTS_ENABLED`, off by default.** Exactly the reasoning behind
`REPORT_EMAIL_ENABLED` — the same Google authorization powers the Sheets export, and
connecting Google for sheets must not silently start emailing people. Recipients from
`BUDGET_ALERT_TO`, falling back to `REPORT_TO`, validated by `clean_recipients` (`:79`).

**Failure must never fail the write.** The hours are the point; the email is a courtesy.
Wrap the send, log `mailer.explain(exc)` on failure, and return success — a Gmail outage
must not stop anyone logging time.

**Cheaper first step:** skip email in phase 1 and let the control centre carry a "3
projects over budget" count in the nav, plus the live meter on the log form. Most of the
value of a notification here is on-screen, and it costs no credentials and no send path.

---

## 7. Build order

Each phase is independently shippable and leaves the app working.

| Phase | What | Touches |
| --- | --- | --- |
| **0 — Schema** | `ensure_budget_properties()`; `list_projects` returns `budget`; TTL cache. Nothing enforced, nothing shown. Budgets can be typed into Notion by hand and read back. | `notion_ops.py`, `app.py` `_startup` |
| **1 — Control centre** | `GET /budgets` + `/budgets.csv` + nav entry + `POST /api/budget` inline editing. **The whole "where are we vs budget" ask, delivered.** | new `budgets.html`, `app.py`, `notion_ops.py` |
| **2 — The meter** | `GET /api/budget/status` + the live line on the log-hours form. Warning at the moment of typing. | `form.html`, `app.py` |
| **3 — Enforcement** | `BudgetExceeded` in the three ops functions; per-route error rendering; the reduce-is-always-allowed rule; CLI `--over-budget` flag. | `notion_ops.py`, `app.py`, `week.html`, `project.html`, `src/log_hours.py` |
| **4 — Alerts** | `mailer.send_plain`, `Budget notified` stamp, `BUDGET_ALERTS_ENABLED`. | `mailer.py`, `notion_ops.py` |
| **5 — Later, if asked** | Per-month overrides db · whole-project (non-monthly) budgets · `show_budget_to_all` for non-admins. | — |

Phases 0+1 are the ones actually asked for ("a control centre that tells me every project
and where we are with the hours tracked vs the budget"). 3 and 4 are the "different
logics" and the "notification when reaching budget" — worth building, but they're the
parts that can misfire, and they're much safer once real budgets have been sitting in
phase 1 for a couple of weeks and the numbers have been sanity-checked against reality.

## 8. Config summary

| Env var | Default | Meaning |
| --- | --- | --- |
| `BUDGET_WARN_PCT` | `95` | Fallback threshold when a project's `Warn at %` is blank |
| `BUDGET_ALERTS_ENABLED` | off | Master switch for budget emails, separate from Google creds |
| `BUDGET_ALERT_TO` | falls back to `REPORT_TO` | Recipients |

No new database, so **no `databases.json` / Render id wiring** — the one piece of setup
work this feature doesn't need.

## 9. Open questions

1. **Should a hard cap stop admins too?** The plan says yes — the admin's escape hatch is
   raising the budget on `/budgets`, which leaves a trace, rather than a silent override
   that doesn't. Worth confirming, since it's the decision most likely to annoy someone
   at 6 pm on a Friday.
2. **Is the budget per calendar month, or per invoicing month?** They're the same today.
   If a client's month ever runs 15th–14th, this needs to know.
3. **Do the ~37 active projects have known budgets to seed?** Only one Notion project
   name carries an hours suffix (`Vital Signals - OSS (80h)`) — the rest of the
   suffixes live on the Harvest-side names (`SaltWorks - OSS (40h)`,
   `Streamside OSS - 60h (2026)`, per `docs/harvest-sync.md`). So budgets will be typed
   in on `/budgets` rather than parsed out of names, and parsing names would be a bad
   idea anyway: it would silently re-derive a budget every time someone renames a project.
