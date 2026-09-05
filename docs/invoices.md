# Invoicing a month's hours

Viewing **one project** for a **month**, the button on `/project` reads
**Invoice** instead of Export and opens the same screen. There the hours appear
in two columns — **Tracked** (what Notion holds, read-only) and **To bill**
(editable, defaulting to tracked) — and saving records the month against that
project. `/invoices` lists what's been billed. Admins only, like the rest of
`/project`.

**Two ways to start one.** From `/project`, where you were already looking at
the month. Or from `/invoices` itself: a **＋ New invoice** picker in the header
(a project, a month — defaulting to the one that just ended) that GETs
`/project/export?project=…&period=monthly&start=YYYY-MM`, the same URL the
Invoice button links to. That picker exists because the route to a *first*
invoice used to be written only in the list's empty state — so the moment there
was one invoice on file, the page stopped saying how to make the next one. The
picker offers **active** projects only; the filter beside it still lists every
project, including archived ones, since old invoices have to stay findable.

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
| `Number` | rich text | `2026-014` — assigned once and then kept |
| `Rate` | number | the hourly rate **as billed**, copied off the project at save time |
| `Amount` | number | `Hours billed × Rate`, pre-tax |
| `Currency` | rich text | copied off the project too |
| `Client note` | rich text | printed on the PDF, unlike `Note`, which is internal |
| `Sent to` / `Sent at` | rich text / date | written only after Gmail accepts the message |

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

## The PDF, and sending it to a client

`/invoices/{id}.pdf` builds the document itself (`web/invoice_pdf.py`), and
**✉ Send to client** on the invoice mails that exact file. The download and the
send go through one helper (`_invoice_document` in `app.py`) precisely so the
file a client receives is byte-for-byte the one an admin previewed.

The document is a bill, not a report: a company block, an addressee, a numbered
header with issue and due dates, one line per person (hours × rate = amount), a
total, and — on a second page — every billed entry with its date, person and
comment, so a client can check the total without asking for a spreadsheet. The
lines are `_invoice_export_rows`, the same billed rows the workbook and the
clipboard get, which is why the three can't disagree about what was billed.

### Money: three places, on purpose

| What | Lives in | Why there |
|---|---|---|
| Hourly `Rate`, `Currency` | the **project**, in Notion | it's a fact about the client relationship, curated where every other project fact is |
| `INVOICE_TAX_PCT`, company, address, payment terms | the **environment** | it's a fact about *us*, identical on every invoice, and changing entity shouldn't need a deploy |
| The rate this bill was actually cut at | the **invoice row** | copied off the project when the invoice is saved |

That last one is the important one. A rate that changes next quarter must not
restate a bill already sent, so `save_invoice` copies rate and currency onto the
row rather than the PDF looking them up when it's drawn. The export screen
defaults the Rate field from the project and lets it be typed over — a
discounted month is common enough that forcing an edit in Notion first would
just mean the rate gets left wrong.

**A project with no rate still produces a valid document.** The money columns
aren't drawn and it reads as a statement of hours. Nobody was going to fill a
rate in for 35 projects on deploy day, and an invoice printing "0.00" would be
worse than one printing none. The invoice page says so and links back to
re-invoice once the rate is set.

### Numbering

`Number` is a per-calendar-year sequence — `2026-014`, with an optional
`INVOICE_NUMBER_PREFIX` in front — derived from the numbers already filed rather
than from a counter, because a counter would be a second source of truth living
outside Notion and someone always renumbers a row by hand. It's assigned under
the same write lock that does the upsert, so two invoices saved at once can't
collide, and **a correction keeps the number it already had**: re-saving July
after someone logs late is the same bill, not a second one.

### The client's address

Also curated in Notion, on the project: `Client name`, `Client email`,
`Client address`, added to the Projects db on boot by
`ensure_billing_properties` the way the budget columns are. The send box
prefills To from `Client email` and stays editable; when the project has none it
says so rather than offering an empty field with no hint of where the address is
supposed to come from.

### The switch

Emailing invoices rides `INVOICE_EMAIL_ENABLED`, **not** the report's
`REPORT_EMAIL_ENABLED`. One Google authorization powers three features now
(Sheets export, report email, invoice email) and this is the only one that sends
outside the company — turning on an internal report email must not arm a button
that mails a bill to a client. Downloading the PDF works with the switch off.
The transport is the Gmail API over HTTPS for the reason `mailer.py` explains:
Render's free instances block the SMTP ports outright.

`Sent to` / `Sent at` are written only **after** Gmail accepts the message, so a
row can't claim a send that failed — and if that write fails afterwards it's
logged rather than failing the request, since the client already has the
invoice and a 502 would only invite a second send. Nothing is queued or
retried.

## Not in scope

No line-item discounts, no multi-currency conversion, no payment status
(draft / sent / paid — `Sent at` is as far as it goes), no PDF logo. A logo is
one image and an env var whenever it's wanted; the rest are decisions nobody
has needed to make yet.

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
- The PDF, end to end against real Notion: saving with a rate filed
  `2026-014` / rate 45 / amount 382.50; re-saving at a different rate **kept the
  number and the page**; the document rendered with the company block, bill-to,
  line items, VAT and total, and a second page listing every billed entry; a
  project with no rate produced the same document without money columns; a month
  billed at nothing produced a valid one-page file rather than an error.
- The guards: `/api/invoice/send` answers 403 while `INVOICE_EMAIL_ENABLED` is
  off, and the To/Subject/Send controls aren't rendered at all — the same rule
  the export screen follows. With the switch on, the send box appears, names the
  Cc, and says which project has no `Client email` in Notion.
- Driven in a real browser: typing a rate on the export screen updated the
  header live (*tracked 20.75 h · billing 20.75 h · $1,129.84 incl. VAT*), and
  the confirm dialog names the money as well as the hours.
