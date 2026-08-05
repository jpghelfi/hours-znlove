# Invoicing a month's hours — analysis

*Written 2026-08-05. Design note, nothing built yet.*

## The ask

Admins only. Viewing **one project** for a **month**, the export button reads
**Invoice** and opens the screen we already have. There the sender sees two
hour columns — **tracked** and **to bill** — which are often the same and
sometimes aren't. Saving records that month as invoiced, with the date it was
created, and a list of invoices shows what was billed per project per month.

## The one thing this changes conceptually

The export screen's whole premise today is that **nothing is saved**. It says so
in a banner, and that's load-bearing: it's what makes it safe to round a number
down for a client without corrupting what someone actually logged.

Invoicing keeps that promise but adds a third layer. Worth naming them, because
every question below resolves against this:

| Layer | What it means | Writes to |
|---|---|---|
| `/api/entry/hours` on `/project` | fix what was **logged** — someone typed 8 when they meant 6 | the Time Entry |
| The export screen | what the client **sees** this time — ephemeral | nothing |
| **Invoice (new)** | what we **billed**, as a record — permanent | a new Invoices row |

An invoice still never rewrites a logged entry. It records a decision *about*
those entries. The banner needs rewording, not removing: "adjustments don't
change what was logged" stays true; "nothing here is saved" stops being true.

## Where it's stored: a new Notion Invoices database

Notion stays the source of truth, so this is a database under the Hours Tracker
page, created by an idempotent `src/setup_invoices_db.py` and wired into **both**
id paths in `src/config.py` (`databases.json` locally, `INVOICES_DS_ID` /
`INVOICES_DB_ID` on Render) — the standing rule for any new database.

| Property | Type | Why |
|---|---|---|
| `Invoice` | title | `Auter — July 2026`, built from project + month |
| `Project` | relation → Projects | the real link, so a rollup of billed hours per project works in Notion |
| `Month` | date | the **1st** of the month, so it sorts and filters as a date rather than as text |
| `Hours tracked` | number | what was logged when the invoice was saved |
| `Hours billed` | number | what we charged for |
| `Issued` | date | *the* date asked for — defaults to today, editable, because an invoice for July is often cut in August |
| `Saved by` | people | who pressed the button |
| `Note` | rich text | optional, one line ("wrote off 3h of rework") |

`Client` already exists on Projects as **rich text**, so the client name comes
along for the title and any future grouping — but grouping by client would be
string-matching until it becomes a relation. Fine for now, worth knowing.

**Deliberately not stored: the per-row breakdown.** The ask is "per project and
monthly hours for now", and totals are what the list shows. The cost is that
reopening a saved invoice can't show *which* rows you adjusted — it re-reads
Notion and re-derives. If that matters later, the cheap fix is a per-person
breakdown (bounded, a handful of rows) rather than every entry.

## The screen

Two changes to `project_export.html`:

1. **Two hour columns.** `Hours tracked` read-only (straight from Notion) and
   `To bill` editable, defaulting to tracked. The existing single editable
   `Hours` input becomes `To bill`, so the code path barely moves —
   `_rows_from_payload` grows a second validated number, and `to bill = 0` keeps
   its current meaning: the row drops out.
2. **A Save/Invoice action** next to Download/Send, showing the running totals:
   *tracked 142h · billing 138h*. The difference is the number anyone actually
   wants to see before pressing save.

The client-facing file should show **billed hours only** — the tracked column is
an internal working number, and putting both in front of a client invites an
argument about the difference. (Say the word and it's one flag either way.)

## When the button says "Invoice"

Only when both are true:

- exactly **one project** is picked (an invoice for "3 projects" isn't a thing), and
- the period is **monthly** (the ask is monthly hours; a daily invoice isn't one).

Otherwise it stays **Export**, unchanged. That's a two-condition template
change, and the invoice POST re-checks both server-side rather than trusting the
button that was rendered.

## Saving

`POST /api/invoice` → `ops.save_invoice(project_id, month, rows, issued, note)`.

**Upsert on (project, month)**, the same discipline as `set_cell`: saving July
for Auter twice updates the row rather than filing a second invoice, because
the second one is nearly always a correction, not a second bill. The screen
should say so plainly when it finds an existing invoice — *"Auter, July 2026 was
invoiced on 3 Aug for 138h. Saving replaces it."* — rather than silently
overwriting. `Issued` keeps its original value on a re-save unless it's edited;
Notion's own page history covers the audit trail.

## The list

`/invoices`, admins: one row per invoice, newest month first — project, month,
tracked, billed, the difference, issued date, who saved it, and a link back to
that month's report. A project filter reusing the existing
`_project_filter.html` partial. That's v1; no CSV of invoices, no totals by
quarter, unless you want them.

**One thing worth adding while it's cheap:** because `Hours tracked` is stored,
the list can flag *"July's logged hours have changed since this was invoiced
(142 → 147)"*. That situation is guaranteed — someone always logs late — and
without the flag nobody finds out.

## Explicitly not in scope

No rates, amounts, currency, tax or invoice numbers: this records **hours
billed**, not money. Projects has no rate field today, so adding money later
means a rate on the Project (or per person), plus deciding whose rate wins when
several people work one project. Cleanly separable — nothing here blocks it.

## Effort

Roughly 450–550 lines: setup script + config wiring (~90), `notion_ops` invoice
read/write (~130), routes (~80), export screen (~70), the list page and template
(~140). Bigger than the ticket linking, mostly because of the new database and
the new page.

## Decisions I need from you

1. **Re-saving a month replaces the invoice** (my recommendation) — or should
   each save keep a version, so you can see it was 138h then 142h?
2. **Client file shows billed hours only** (my recommendation) — or both columns?
3. Invoice button only for **one project + monthly** — agreed?
4. Do you want a **status** (draft / sent / paid) now, or is "it exists" enough
   for v1? Adding it later is a one-property change.
5. Should the invoice screen let you edit **`Issued`**, or always stamp today?
