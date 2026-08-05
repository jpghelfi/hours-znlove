# Linking a Notion task to a tracked hour entry — analysis

*Written 2026-08-05. Design note, nothing built yet.*

## What's being asked

When logging hours, optionally attach the Notion ticket the work was for. Two ways in:
a search box that finds tickets by name, or pasting a Notion ticket URL. The link
then travels with the entry — visible in reports and in the client-facing export.

## The blocker to clear first: the integration can't see your tickets

The app reads and writes Notion with one internal integration token (`NOTION_TOKEN`).
An internal integration only sees pages **explicitly shared with it**. Right now it sees
exactly four data sources — everything under the Hours Tracker page:

```
Allocations    b75e776e-…
Time Entries   22a4c841-…
Projects       7609423c-…
People         f6c8e667-…
```

No tasks/tickets database is among them. Until the ticket database is shared with the
integration (Notion → open the ticket DB → ••• → Connections → add "Hours Tracker"),
**both** entry paths fail identically: search returns nothing, and a pasted link 404s
on `pages.retrieve`. This is a one-click change by a Notion admin, but it is a
prerequisite, not a detail — and it's the only part of this that isn't code.

Second-order consequence worth deciding on deliberately: sharing the ticket DB gives
this app read access to every ticket in it, and the app searches with the *integration's*
eyes, not the logged-in user's. Someone could therefore find and link a ticket they
personally can't open in Notion. Inside one company that's usually fine — but it's a
real widening of what this app can see, so it should be a conscious yes.

## Where the link gets stored

Two options on the Time Entries schema. They are not equivalent.

### Option A — a real `Task` relation (recommended if tickets live in ONE database)

Add a relation property on Time Entries pointing at the tickets data source.

- Notion-native: the ticket page gets a backlink, and you can put a **rollup on the
  ticket showing total hours logged against it** — which is the thing people actually
  want out of this, and it comes free, in Notion, with no work in this app.
- Filterable in the Notion query (`{"property": "Task", "relation": {"contains": id}}`),
  same as Project is today — so "all hours for ticket X" is one cheap query.
- Hard limit: **a relation targets exactly one data source.** If znlove's tickets live
  in several boards (one per client/project), a single relation cannot span them.
- A dual (two-way) relation writes a new column into the tickets database, visible to
  everyone in that workspace. A single (one-way) relation doesn't, but then no rollup.

### Option B — `Task URL` (url) + `Task` (rich text, cached title)

- Works for tickets scattered across any number of databases, and even for links the
  integration can't resolve (store the URL, skip the title).
- No backlink, no rollup, no relation filter — grouping by ticket becomes a Python
  string-match on the URL.

### Recommendation

Ask which shape the tickets are in first. If it's one board: **Option A**, dual relation.
If it's many: store both — resolve the pasted/selected page, and if its parent matches a
configured tickets data source, write the relation; always write URL + title as well. The
extra two properties cost nothing and make the export readable without a second Notion read.

## Finding the ticket

### Search box (the main path)

`GET /api/tasks/search?q=…` → HTMX-debounced (300 ms, min 2 chars), returns a small
`<ul>` of results, same shape as the filters already in `web/templates/`.

Implementation: `data_sources.query` on the tickets DS with a title `contains` filter,
sorted by `last_edited_time` desc, `page_size` ~10, and — if the board has a status
column — an `and` clause dropping Done/Archived so the list stays useful. One round
trip, ~200–400 ms.

Do **not** use Notion's `search` endpoint for this if there's a single tickets DB: it's
workspace-wide, matches titles only, and has no parent filter, so you'd fetch broadly
and discard in Python. It becomes the right tool only in the many-boards case, where
you'd filter results against an allowlist of parent data source ids.

### Paste a link (the escape hatch)

Extract the trailing 32-hex id from the URL (`…/Ticket-name-<32hex>`, also `?p=<id>`
for side-peek links), strip any `#block-id` fragment and `?pvs=` junk, then
`pages.retrieve` to get the real title and parent.

Failure modes to handle explicitly, each with its own sentence:
- 404 → "that page isn't shared with the Hours Tracker integration" (the common one)
- URL is a database/view, not a page → "that's a board link, open the ticket itself"
- a valid page whose parent isn't a known tickets DS → accept as URL-only, or reject,
  depending on which storage option is chosen

## Where the picker appears

| Surface | Verdict |
|---|---|
| `/entry` log form | **Yes** — the primary place. An optional "Link a Notion task" button under Description, opening a popover with the search box + a paste field; picking one shows a removable chip and fills hidden `task_id` / `task_url` / `task_title` inputs. |
| Timer flow | Free — the timer only fills the Hours field on the same form. |
| `/week` grid cells | **No, not in v1.** A cell is an upsert keyed on (person, project, date) and can aggregate several sittings; which ticket would it carry? Adding it here means designing multi-entry-per-cell first. |
| `/project` entry list | **Yes, read-only** — show the ticket as a link next to each row's Description. |
| Exports (csv / xlsx / gsheet / email) | **Yes** — a Task column. This is the client-facing payoff: "these 3h went to *this* ticket". |

## What has to change in the code

- `web/notion_ops.py`
  - `ensure_task_property()` alongside the existing `ensure_person_property` /
    `ensure_admin_property` startup hooks — creates the property if missing.
    (The 2025-09-03 relation-creation payload keys off `data_source_id`, not
    `database_id`; verify that against a scratch database before pointing it at
    Time Entries.)
  - `create_entry(...)` grows optional `task_id` / `task_url` / `task_title`.
  - `entries_between` and `project_entries` return the task fields — tolerantly.
    Both read properties by name, and a rename in the Notion UI has taken pages down
    here before (see the `val` incident in CLAUDE.md — and note the `Logged by` column
    is *currently* renamed to `melisa`, so Notion-form entries are already losing their
    submitter). Read the task property through the same schema-resolution trick
    `alloc_person_prop` uses, or at minimum `.get()` everything.
  - `search_tasks(q)` — the query above, plus `resolve_task_url(url)`.
- `web/app.py` — `GET /api/tasks/search`, `POST /entry` accepting the three new fields
  (validating that `task_id` is a real page in an allowed DS, exactly as
  `set_entry_hours` refuses pages whose parent isn't Time Entries — the id is
  client-supplied), and the Task column threaded into `_period_entries` consumers.
- Templates — a new `_task_picker.html` partial included by `form.html`; a link column
  in `project.html` and `project_export.html`.
- `web/report_xlsx.py` / `report_gsheet.py` / the CSV route — one extra column each.
- Config — `TASKS_DS_ID` (or a comma-separated list), wired into both the
  `databases.json` and env paths in `src/config.py`, per the existing rule that new
  databases must land in both.

Roughly 250–350 lines across those files. No migration: old entries simply have no
task, and every reader must treat the property as optional forever.

## Open questions (these change the design, not just the effort)

1. **One tickets database, or one board per client/project?** Decides relation vs URL,
   and search-endpoint vs data-source-query. This is the question to answer first.
2. **May the app write a backlink column into the tickets database?** (dual relation →
   hours-per-ticket rollup for free; single relation → nothing lands in their board)
3. Should the ticket be *required* for some projects, or optional everywhere? Optional
   everywhere is assumed above.
4. Ticket on the weekly grid later, or is the log form enough?
