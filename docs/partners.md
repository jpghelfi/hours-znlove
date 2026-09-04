# Partner umbrellas

Some clients are ours directly. Others arrive under a partner agency: Bear
brings Streamside, True Temper and its own work; Telus brings its own. Both
kinds of client are ordinary projects — the partner is the extra fact, and it
is one column in Notion.

## The model

Two properties on the **Projects** data source, added by
`ops.ensure_partner_properties()` on every boot and by
`src/setup_partners.py`:

| Property | Type | Means |
| --- | --- | --- |
| `Partner` | select | the umbrella this project sits under. Empty = a direct client. |
| `Umbrella` | checkbox | this row **is** that partner — the project its own hours book against. |

There is deliberately no Partners database. A partner has no attributes beyond
its name and the projects under it, so a select column curated in Notion — like
`Active`, like the budget columns — is the whole model. Adding a partner is
adding an option in Notion (or `--add-partner`); the app never invents one,
because Notion silently *creates* a select option for any name it is handed and
a typo would file a real client under a third agency that doesn't exist. That is
the same trap `create_ticket` documents for the ticket board's project column.

## The two things a partner is

These are different, and keeping them apart is most of the design.

**A filter.** `?partner=Bear` means *whatever is under Bear today*. It is
resolved to project ids per request (`_scope_keep_ids` in `web/app.py`), so a
saved link, a bookmark or an emailed report keeps working when a project joins
or leaves the umbrella. Ticking Bear's five projects individually would freeze
that list; ticking Bear does not. The partner pick lives in the shared project
picker (`web/templates/_project_filter.html`) rather than in a second dropdown
beside it, because a partner is a way of naming a set of projects. It composes
with the project pick, the PM/AM pick and everything else by AND: tick Bear,
then tick two of its projects, and you see those two.

Carried by `/project`, `/project.csv`, `/project.xlsx`, `/project/export`,
`/reports`, `/reports.csv`, `/budgets`, `/budgets.csv`, `/invoices` and
`/schedule`. Exports name the partner: a Bear rollup downloads as `bear_…`, not
`all-projects_…` (`_export_label`).

**An umbrella project.** One real Projects row per partner, ticked `Umbrella`,
that hours and allocations can be booked against when the work is the partner's
own rather than any one client's. It has to be a real project, because a Time
Entry's and an Allocation's `Project` is a *relation* into the Projects data
source — a select value is not bookable. It is titled after the partner with
nothing appended (`Bear`, not `Bear (umbrella)`): that name ends up in reports,
invoices and client-facing exports, where "Bear" reads and the parenthetical
doesn't. The tick is what tells them apart, and it is what the UI badges.

The umbrella row carries its own `Partner`, so it sits *inside* the Bear filter
next to Bear's client projects. Booking "Bear" and booking "Streamside" both
roll up under Bear — that is the point.

## Where it shows up

- **`/schedule`** — a partner dropdown beside the project one, and the project
  dropdown itself is grouped by umbrella (with each partner's own project first
  in its group). The assign popover groups the same way, so "this week is
  Bear's, not any one client's" is one pick. A third grouping, **Partners**,
  gives each umbrella a row; it is a **rollup, not a planner** — a row spanning
  several projects can't say which one a click would book, so its cells render
  read-only, the same treatment a non-admin gets. Planning happens in the People
  and Projects groupings, where the umbrella is just another project.
- **`/project`** — a *By partner* card above the table, and each project row
  says which umbrella it is under. Picking a partner titles the page after it.
- **`/reports`** — a *By partner* card, each bar linking to that partner's view.
- **`/assignments`** — the Partner column, where a project is moved under an
  umbrella (`POST /api/project/partner`). This is the one page that answers "how
  is this project set up", so it belongs beside the PM and the people.
- **`/budgets`, `/invoices`** — filter only.

## The destructive-write catch

`/schedule`'s bulk buttons — Copy last week, Clear week, the day and cell wipes
— promise to touch **exactly what is on screen**. A partner filter the delete
couldn't see would wipe the whole company's week off a screen showing one
partner's, so `copy_week_allocations` and `clear_allocations` take a
`project_ids` list and the endpoints resolve `partner` to those ids
*server-side* (`_partner_project_ids`), never from the browser. An unknown
partner name resolves to the empty list: nothing matches, the write does
nothing, which is the right way for a stale filter to fail.

## Setup

```bash
# create the columns (seeded Bear + Telus), then an umbrella row per partner
python src/setup_partners.py

# file projects under an umbrella
python src/setup_partners.py --partner Bear "Streamside OSS" "True Temper"

# take one back out
python src/setup_partners.py --partner "" "Fotosprint"

# a new partner: the option first, then what goes under it
python src/setup_partners.py --add-partner Acme --partner Acme "Some Project"
```

Idempotent — re-run it whenever a partner is added. The seeded option list comes
from `PARTNERS` (default `Bear,Telus`) and is only ever read when the column
doesn't exist yet; after that Notion's own option list is the source of truth.

Day to day none of this is needed: partners are edited on `/assignments` or in
Notion directly.

## What it deliberately doesn't do

- **No rates, no margins.** A partner is a grouping, not a contract.
- **No nesting.** A project has at most one partner; partners don't have
  partners.
- **No effect on who can log what.** Membership is still the People property on
  each project.
