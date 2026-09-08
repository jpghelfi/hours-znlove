# Absences — who's off, and when

`/absences` does two things on one screen: log that you're away, and read who's away. It's open to everyone, not just admins — the point of it is that people file their own.

## Logging one

The form asks for a first day, an optional last day, and a reason. It never asks **who**: an absence is always filed for the logged-in person, the same way `/api/cell` only ever writes the caller's own hours. Leave the last day blank and it's a single day off.

Every absence is filed **Pending** and waits for one approver's OK — including an approver's own, and including an admin's. The form says so, and the flash after a save reads "Sent for approval".

`POST /absences` is a plain form post that redirects back to the view it was filed from (`period` + `anchor` + any people picks ride along as hidden inputs), with `ok=` or `err=` carrying the outcome. Everything it refuses — a backwards range, a blank reason, a range longer than a year — comes back as a sentence above the form rather than a 400.

## What a row is

**One row per absence**, holding the whole stretch in a single Notion `Dates` property (start + end). Not one row per day, which is how allocations work:

- an absence is one decision with one reason — writing the reason on ten rows says the same thing ten times;
- removing it should be one click, not ten;
- a range in one date property is what Notion's calendar and timeline views want.

`Days` is stored alongside, holding the **weekday** count, so the Notion table shows what an absence costs without anyone counting on their fingers. Weekends never count — a Friday-to-Monday absence is two days, not four (`ops.weekdays_between`).

| Property | Type | Notes |
| --- | --- | --- |
| `Absence` | title | `<name> · 10 Aug – 14 Aug 2026` |
| `Person` | people | who is off |
| `Dates` | date | start, plus `end` for a range |
| `Days` | number | weekdays covered |
| `Reason` | rich text | capped at `MAX_ABSENCE_REASON` (400) |
| `Status` | select | `Pending` · `Approved` · `Declined` — three fixed options, never a fourth |
| `Decided by` | people | who approved or declined it |
| `Decided at` | date | when |
| `Decision note` | rich text | optional, capped at `MAX_ABSENCE_NOTE` (400) |

The four approval columns are added on boot by `ops.ensure_absence_properties()` (the `ensure_budget_properties` shape — read the schema, add what's missing, guarded on `ABSENCES_DS`), so an existing Notion db catches up without the setup script being re-run.

Created by `src/setup_absences_db.py` (idempotent). On Render set `ABSENCES_DB_ID` and `ABSENCES_DS_ID`; both id paths, as always. Until then `ops.absences_enabled()` is false, the page still renders, and it says what to run.

## Approval

### Who approves

**Its own checkbox in the People db**, `Approves absences` — not the Admin tick. There are six admins and exactly two approvers (Zarco and JP), so "may see team-wide reports" and "may sign off a holiday" are different questions and get different columns. An approver need not be an admin, and an admin doesn't approve.

`ops.access_ids()` returns a third set, `approvers` (Active **and** ticked), on the same ~60 s TTL cache as `allowed`/`admins` — so a tick in Notion takes up to a minute to bite. `auth.is_approver(user)` matches the linked Notion user id, OR'd with `ABSENCE_APPROVER_EMAILS` (default `zarco@znlove.xyz,jp.ghelfi@znlove.xyz`), exactly the way `ADMIN_EMAILS` backs up the Admin tick: a People-db misconfig must not leave a queue nobody can answer. `ops.ensure_approver_property()` adds the column on boot; `src/set_absence_approvers.py "Name" …` ticked it the first time (idempotent, and it unticks nobody — revoking is a deliberate act in Notion).

### The lifecycle

```
filed ──> Pending ──> Approved   (counts as time off)
              └────> Declined    (never reaches the board)
```

`POST /api/absence/decide` takes `{absence_id, decision, note}`, refuses anyone who isn't an approver with a 403, and anything that isn't `approved`/`declined` with a 400. The id comes from the browser, so the page's parent data source is checked inside the write lock **before** the write — the same guard `delete_absence` uses.

**A decision is correctable.** Approved → declined and back is the same decision being fixed, not a second flow: the row keeps one `Status` and names whoever touched it last. So the buttons on a row are always the *other* answer — an approved row still offers Decline. Declining asks for an optional note (its own small `<dialog class="zdlg">`, built like `zConfirm`: plain buttons that call back directly, the `cancel` event covering Esc); approving asks `zConfirm`.

Removing is unchanged and independent of status: the owner may withdraw a request at any point, and an admin can still remove anyone's.

### Legacy rows read as Approved

A row with **no** `Status` — or with a status name the app doesn't know, or with the column renamed out from under it — reads as **approved**. Those rows were filed under the old "you log it, nobody signs it" rule, and putting them in a queue nobody knew they had joined would drop real days off the board. Every read goes through `.get()` for the same reason `_budget_from_props` does (the `alloc_person_prop` lesson): a renamed column degrades to the old behaviour, never to a 500. Nothing was backfilled.

### The queue

Approvers get an **Awaiting approval · N** card above everything else on `/absences`, listing every pending request **across all time** (`ops.list_pending_absences()` — one query filtered on `Status`, sorted by date, deliberately with no period bound: a December request filed in September has to be answerable in September, and a stale one is still something to clear). It renders at N = 0 too, so an approver learns the queue exists before the first request lands in it. Non-approvers never see it — but the subtle line under the heading shows everyone a pending count: their own, in this period.

### The emails

Two, both a courtesy and both logged-and-swallowed the way `_maybe_alert_budget` is — the absence is already filed and the queue is the real notification, so a mail failure never turns a save into an error:

- filing one mails the approvers (`ABSENCE_APPROVER_TO`, defaulting to the same two addresses);
- deciding one mails the person who asked, at the address on their Notion profile, naming the decider and any note.

Both ride **`ABSENCE_EMAIL_ENABLED`**, their own switch — the same reasoning as `BUDGET_ALERTS_ENABLED` and `INVOICE_EMAIL_ENABLED`: one `GOOGLE_*` authorization powers every send this app makes, so connecting Google for the Sheets export must not start mailing two people every time somebody books a Friday off. `mailer.send_plain(..., channel="absence")` picks the transport and names the right variable when it refuses.

## Reading it

### The overlap query

`ops.list_absences(from, to)` returns every absence **overlapping** the period, not only those inside it: a fortnight off that started in June is still what someone is doing on the 1st of July.

Notion's date filters compare against a range's *start*, so "ends on or after X" can't be asked for server-side. The query instead reaches back a bounded window (`_MAX_ABSENCE_DAYS`, 366) before the period and settles the far edge in Python. The bound is what keeps that read small.

### The board

One period at a time — a week or a month — with the same prev/now/next toolbar as `/project`, and `_period_range` reused wholesale.

| Period | Columns | A cell |
| --- | --- | --- |
| **Weekly** | Mon–Fri | ● approved · ○ awaiting approval |
| **Monthly** | the weeks touching the month | days off that week · `n?` still awaiting |

**Only approved days count.** The totals under the board, `days_off` and `people_off` are a statement about who won't be at their desk, and a request nobody has answered isn't that yet — so a pending day is drawn *hollow* rather than filled, and adds nothing to any number. Declined rows never reach the board at all; they stay in the list, where the decision note explains itself. Someone whose only absence in the period is pending still gets a row, because the person waiting and the approver reading the week both need to see it coming.

A month of weekday columns would be 22 of them, so the monthly view buckets into weeks — the same Days/Weeks split the schedule makes.

`_absence_days` expands each row into weekdays **clipped to the period**, keyed `person → {date: reason}`, **one status at a time** — the board runs it twice, once for approved and once for pending, because those two are different statements. Two consequences worth knowing: an absence straddling the period edge counts only the days inside it (while the list underneath still shows the whole thing), and two absences overlapping on one day count that day **once** — a dict of dates can't double-count.

### Scope

Approving is its own scope, orthogonal to this one: an approver sees the queue, an admin sees the team. Admins see everyone and get the shared people filter (`_people_filter.html`, the same one `/reports` and `/schedule` use). Everyone else sees only their own, and the filter isn't drawn — the pick is dropped server-side too, not just hidden.

## Removing one

`POST /api/absence/delete` archives the page. Two checks happen inside the write lock **before** the write, because the id comes from the browser: the page's parent must be the Absences data source (the same guard `set_entry_hours` uses), and the caller must own the row — `any_person=True` is the admin's escape hatch. Otherwise anyone who could read an id could cancel someone else's holiday.

The board above the list is a server-rendered aggregate, so a successful delete reloads the page rather than leaving stale totals next to a row that's gone.

## What it deliberately doesn't do

- **No allowance or balance.** It doesn't know how many days you get — approval is a judgement, not a subtraction.
- **No escalation, reminder or second approver.** One approver's word settles it, and nothing chases them; the queue sitting there is the whole mechanism.
- **No effect on the schedule or reports.** `/schedule` will still let you book someone who's away — approved or not — and absent days don't appear in capacity math. Wiring the two together is the obvious next step, and is a separate decision from recording the days.
