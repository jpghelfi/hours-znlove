# Invoicing a month's hours

Viewing **one project** for a **month**, the button on `/project` reads
**Invoice** instead of Export and opens the same screen. There the hours appear
in two columns — **Tracked** (what Notion holds, read-only) and **To bill**
(editable, defaulting to tracked) — and saving records the month against that
project. `/invoices` lists what's been billed. Admins only, like the rest of
`/project`.

## The three layers, and why this doesn't break the second one

The export screen's premise has always been that nothing is saved, which is what
makes it safe to round a number down for a client. Invoicing adds a layer rather
than removing that one:

| Layer | Means | Writes to |
|---|---|---|
| `/api/entry/hours` on `/project` | fix what was **logged** — someone typed 8 meaning 6 | the Time Entry |
| The export screen | what the client **sees** on this send — ephemeral | nothing |
| **The invoice** | what we **billed**, as a record | an Invoices row |

An invoice never rewrites a logged entry. The banner says so, and the invoice box
repeats it: *"saving an invoice records what you billed, not a correction to
anyone's timesheet."*

## The Invoices database

Created by `src/setup_invoices_db.py` (idempotent) under the Hours Tracker page,
and wired into both id paths in `src/config.py` — `databases.json` locally,
`INVOICES_DB_ID` / `INVOICES_DS_ID` on Render.

| Property | Type | Note |
|---|---|---|
| `Invoice` | title | `Fotosprint — August 2026` |
| `Project` | relation → Projects | so hours-per-project rollups work in Notion |
| `Month` | date | the **1st**, a real date so Notion can sort and filter it |
| `Hours tracked` | number | what was logged when the invoice was saved |
| `Hours billed` | number | what we charged for |
| `Issued` | date | defaults to today, editable — July is usually invoiced in August |
| `Saved by` | people | who pressed the button |
| `Note` | rich text | internal, one line |
| `Adjustments` | rich text | `{entry id: billed hours}` for the lines billed at something other than their logged hours (added on startup by `ensure_invoice_properties`) |

Everything degrades quietly when it isn't configured: `invoices_enabled()` is
False, the Invoice button never appears, and `/invoices` explains what to run
rather than erroring.

## Saving

`POST /api/invoice` → `ops.save_invoice`, which **upserts on (project, month)**.
Saving August for Fotosprint twice corrects that row rather than filing a second
bill, because the second save is nearly always a correction. The screen says
what's already on file before replacing it — *"Already invoiced on 5 Aug for
6.25 h by Juan Pablo Ghelfi. Saving replaces that record."* Duplicate rows from
any old race are folded into one, the same way `set_cell` does it.

**The billed total comes from the screen; the tracked total is re-read from
Notion.** That asymmetry is deliberate: a row zeroed on the export screen drops
out of the file, so trusting the browser's total would quietly shrink what we
claim was tracked. Billed is a human decision, tracked is a fact.

Both the month (must be the 1st) and the project are re-validated server-side —
the button that was rendered is not evidence of anything.

## Opening an invoice: the days as they were billed

Clicking an invoice opens `/invoices/{id}` — the month day by day, showing
**Tracked** next to **Billed** for every entry, with a subtotal per day. A line
billed at less than it was logged is marked; a line billed at nothing is struck
through, because "not on this bill" is a different statement from "small".

Making that possible meant storing something the first version deliberately
didn't. The compromise is to store **only the lines that differ** — an
`Adjustments` property holding `{entry id: billed hours}` for the handful that
were changed, chunked across rich-text objects if a month ever needs more than
one. Opening an invoice then re-reads the month's entries and lays those
overrides back over them.

Two consequences of reconstructing rather than snapshotting, both deliberate:

- The detail shows **today's** entries — current comments, and anything logged
  after the invoice was saved. That's the same reason the list can flag a month
  that has moved, and it's the honest reading: an invoice records what was
  billed, not a frozen copy of the timesheet.
- The adjustments are **rewritten on every save**, so a line billed back at its
  logged hours stops being an adjustment instead of lingering as one.

Invoices saved *before* this existed have no breakdown, and their days would
add up to the tracked figure while the header says something else. Rather than
show that contradiction, the page says so and offers to re-invoice — which
records the breakdown properly.

The invoice id comes out of a URL, so `get_invoice` refuses any page whose
parent isn't the Invoices data source: a project row or a time entry pasted
into the path lands back on the list, not on a half-rendered invoice.

### Sending it on

Two buttons on the invoice, both carrying the **billed** hours with the lines
billed at nothing left out — this is the copy that goes to a client, not the
internal tracked-versus-billed comparison:

- **Download Excel** (`/invoices/{id}.xlsx`) — the same `report_xlsx` workbook
  the reports use, built from the invoiced rows rather than the logged ones.
- **Copy & open Google Sheets** — the rows as TSV plus `sheets.new` for a ⌘V,
  the same clipboard route as the export screen (synchronous `execCommand`
  first, precisely so the click still counts as user activation when the tab
  opens; `navigator.clipboard` as fallback; a blocked popup is reported rather
  than swallowed). Google publishes no URL that creates a spreadsheet holding
  your data, and the app is behind a login so `IMPORTDATA` can't reach it.

`/invoices/{id}.xlsx` is declared before `/invoices/{id}` — a path parameter
happily swallows a `.xlsx` suffix, so the generic route would otherwise win and
render HTML for a download.

## The list

`/invoices`: project, month, tracked, billed, the difference as a chip, issued
date, who saved it, note. Newest month first, filterable by project through the
same `_project_filter.html` partial the other pages use. Each project name links
back to the month it was cut from.

Because `Hours tracked` is stored, the list flags a month whose logged hours have
**moved since it was invoiced** — *"now 8.5 h logged"* against a 6.5 h invoice.
Somebody always logs late, and without the flag nobody would find out. It's
computed for the last four months only, from a **single** read of that window
rather than one query per invoice: a two-year invoice list would otherwise cost
two years of round trips to draw.

## Not in scope

No rates, amounts, currency, tax or invoice numbers — this records **hours
billed**, not money. Projects has no rate field today; adding money later means a
rate on the Project (or per person) and deciding whose rate wins when several
people work one project. Nothing here blocks that.

No status (draft / sent / paid) in v1 — one Notion property whenever it's wanted.

## Verified

- End to end against real Notion: saving files a row with billed from the screen
  and tracked from Notion; a zeroed row widens the gap rather than shrinking
  tracked; saving again updates **the same page** (`replaced: true`, no
  duplicate); the row reads back through `list_invoices` with `Saved by`
  resolved to a name; mid-month dates, missing months and unknown projects are
  all refused with 400.
- Driven in a real browser: the button reads **Invoice** only for one project on
  a monthly view (Export for a day, Export for all projects), the totals update
  live as hours are edited (*tracked 6.5 h · billing 6.25 h*), the confirm names
  the project and amount, and the saved invoice appears on `/invoices`.
- The staleness flag: logging 2 h into an already-invoiced month made the list
  show *"now 8.5 h logged"* against the 6.5 h it was invoiced at.
