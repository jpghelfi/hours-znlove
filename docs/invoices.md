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
| `Lines` | rich text | what the bill is for, one line per row — typed on the invoice page before sending |
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
header with issue and due dates, **one line item whose description is what the
sender typed**, the total hours, the amount, tax and total. Nothing per person
and nothing per entry — the workbook and the Sheets copy are the log, and a
client who wants it gets that file, not a bill that copies it.

### What the bill says is typed on the invoice page

The send box on `/invoices/{id}` is the editor for the document itself, above
the To/Subject it always had:

| Field | Prefilled with | Stored as |
|---|---|---|
| **What the bill is for** — one line per row | `<project> — <Month YYYY>` | `Lines` (rich text, newline-separated) |
| **Total hours** | the invoice's `Hours billed` | `Hours billed` |
| **Amount** (before tax) | `Amount` on the row, or hours × rate when the row predates amounts | `Amount` |

Under the fields the page shows the arithmetic (`n lines · 42 h · $3,000.00 +
VAT $630.00 = $3,630.00`) with the same rules `invoice_pdf.totals` uses. All
three are editable, and **both ways out save them first**: ⬇ Download PDF
posts to `POST /api/invoice/bill` and then fetches the file, and ✉ Send carries
the same fields and writes them before the PDF is built (`_apply_bill` in
`app.py`, `ops.set_invoice_bill`) — so a send that fails keeps the edits, and
the file a client received is the one an admin can download afterwards. The
covering email lists the same lines and the same total, so the note can't
describe a different bill from the one attached. A bill needs at least one
line; lines are capped at 20 of 200 characters.

**The typed hours *are* `Hours billed`.** Typing over the total on the send box
rewrites the invoice's billed hours — it's what was billed — while the
day-by-day breakdown under it keeps saying what was logged. When the two
differ, the page says so in a note rather than leaving a total that the lines
below don't add up to. The typed amount is stored as a real number, including
`0`, and `_invoice_row` returns `None` for a row that never had one — the same
distinction `rate` draws — so a deliberate free month and a pre-amount row
can't collapse into each other. Re-invoicing from the export screen resets
`Amount` to hours × rate (it's a new bill) but leaves `Lines` alone.

The rate is printed on the PDF only while the amount is still hours × rate: a
typed-over amount is the bill, and a rate beside it that doesn't multiply into
it is exactly what accounts payable bounces. An amount typed for a project
with no rate still draws the money columns and is still taxed.

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

Reopening an invoice pins its saved rate, **including a rate of zero**. A month
deliberately billed as hours only stores a real 0, and an invoice filed before
rates existed stores nothing at all — `_invoice_row` returns `None` for the
second so the two can't collapse into each other. Without that distinction,
re-saving a free month to fix its hours would silently pick up whatever rate the
project has acquired since and put a charge on it.

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

That lock is one process's, which is the whole of the deployment today (one free
Render instance, one worker). On more than one worker two saves in the same year
could each read the same highest number; the fix then is a uniqueness check
after the write, not a bigger lock.

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
- The zero-rate distinction: `_invoice_row` reads an unset `Rate` as `None` and
  a saved 0 as `0`, so reopening a month billed as hours only leaves the Rate
  field blank instead of refilling it from the project.
- The printed lines add up to the printed total: with three people whose line
  amounts each round up, the summed lines (200.49) and one multiplication of the
  hours (200.31) differ, and the document now shows the former in both places.
- A `NaN` rate is refused with 400 rather than sailing past `max(0, …)` — which
  it does, since NaN compares false against everything — and failing inside
  Notion's write.
