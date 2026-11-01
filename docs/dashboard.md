# Dashboard (`/dashboard`)

One calendar month for admins, on one page: what needs action, how each
budget is burning, how busy each person is, who has gaps in their time, and
what's tracked but not yet invoiced. Admins only (a non-admin is redirected to
`/`); first item under **Reports ▾** and in the phone's More sheet. Moves a
month at a time with the shared `←` · This month · `→` trio and a month picker
(`_period_range("monthly")`).

## Sections

1. **Needs action** — linked tiles: absences awaiting approval (all time,
   `list_pending_absences`), projects over budget this month (the same
   `over`/`over_cap`/`blocked` set `/budgets` counts), and invoices by stage —
   Draft, Approved ("ready to send"), Sent ("awaiting payment") and Overdue
   (`_invoice_stage`). Invoice counts are over **every** invoice, not just this
   month's: an unpaid bill from March is exactly what needs action.
2. **Budget burn** — budgeted active projects, trouble-first (`_budget_rows`,
   reused from `/budgets`): budget, used, %, and a **forecast**. Unbudgeted
   projects with hours are one line underneath.
3. **Utilization** — every roster person: logged hours ÷ capacity *to date*.
4. **Missing time** — people with elapsed weekdays that have nothing logged
   and no approved absence, worst first, with the dates.
5. **Billing** — each project with hours (or an invoice) this month: its
   invoice and stage (linked), or "Not invoiced" with a link straight into the
   invoice screen; and an estimated value = hours × the project's `Rate`.
   Hidden when the Invoices db isn't configured.

## The rules

- **Elapsed = weekdays from the 1st through yesterday.** Today is still being
  logged: counting it understates pace and flags everyone who hasn't logged
  yet. A future month has none (sections say so); a past month has all.
- **Forecast** = used ÷ elapsed weekdays × weekdays in the month; for a past
  month it's just used; with nothing elapsed it's "—". It's flagged when it
  passes the budget (a 0 h budget with any hours always is).
- **Capacity** = the person's hours per day × elapsed weekdays − their
  approved absence weekdays in that window × hours per day. The month's full
  capacity is shown beside it in small type. Hours per day is the People db's
  `Hours per day` (edited on `/people`), blank = `DAY_TARGET_HOURS` (8) —
  the same `day_cap` rule `/schedule` uses.
- **Utilization bands** (constants in `web/dashboard.py`): under 60 % low,
  90 % or more full, over 110 % over.
- Only **approved** absences count (`_absence_days(…, "approved")`); a pending
  day is still a missing day until somebody approves it.

## Reads

`list_people`, `list_projects(active_only=False)` (budgets and rates ride on
it), one `entries_between` for the month, `list_absences` for the month,
`list_invoices` and `list_pending_absences` — plus the name maps those helpers
read internally. Nothing per project or per person. Absences and invoices are
skipped when their databases aren't configured.

## Where the code is

- `web/dashboard.py` — the pure arithmetic (elapsed days, forecast,
  capacity, utilization, missing days, pipeline). Imports nothing from the
  app, so `tests/test_dashboard.py` runs without `.env` or Notion.
- `dashboard_page` in `web/app.py` — the reads and the glue.
- `web/templates/dashboard.html`.

## Known limits

- Roster members who never log hours (e.g. a founder) show as 0 % and with
  every day missing. If that turns into noise, a `Tracks time` checkbox on
  People is the fix — not an hours/day of 0, which the write refuses.
- "Today" is the server's date (UTC on Render), like every other page.
- No planned-vs-tracked section: deliberately left out.
