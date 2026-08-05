# Linking a Notion ticket to logged hours

An entry can optionally carry the Notion ticket the work was for. It shows up
next to the entry in `/project` and in every export — CSV, workbook, Google
Sheet and the emailed report — so a client-facing report can say *which ticket*
those three hours went to.

Two ways in, and they need very different things from Notion. That asymmetry is
the whole design, so it's worth stating first:

| | Needs from Notion | Works today |
|---|---|---|
| **Paste a ticket link** | nothing at all | yes |
| **Search for a ticket** | the integration connected to the ticket boards | once an admin does that |

## Pasting a link needs no permission

A Notion URL carries the page id *and* the title:

```
https://app.notion.com/p/Fix-checkout-race-condition-3b301234695c813ab615c25e907022cc
                         └────── title slug ──────┘ └──────── page id (32 hex) ───────┘
```

`parse_task_url` in `web/notion_ops.py` reads both without calling Notion, so
pasting works for every page in the workspace whether or not this app can open
it. When the app *can* open the page (`/api/tasks/resolve`), the label is
upgraded to the ticket's real title; when it can't, the slug's version stands
and the user is told where the name came from. Nothing fails either way.

Rules the parser enforces, each learned from a real URL shape:

- **`app.notion.com` is the domain Notion serves today**; `notion.so` is the
  older one still in everyone's bookmarks, and `notion.site` is a published
  page. All three are accepted as whole domain labels — not by suffix, because
  a plain `endswith` would also welcome `evilnotion.com`, and these links get
  clicked out of client-facing reports.
- The id is anchored at the **end** of the last path segment. Searching the
  segment loosely mis-slices it when the slug itself ends in hex-ish text
  (`.../Fix-cafe-<id>`).
- **`?v=` means a board link**, and its trailing id belongs to the *database* —
  accepting it would file hours against a whole board, so it's rejected.
- A side-peek link (`?p=<id>`) carries the **board's** slug in the path, not the
  ticket's, so the id comes from `?p=` and the label is left for the user.
- A `#block-id` fragment never reaches storage.
- A page from **our own databases** is refused (`resolve_task` flags it `ours`):
  linking a time entry or a project row as the "ticket" for some hours is never
  what anyone meant, and it reads perfectly plausibly once stored. Checked again
  on `POST /entry`, since the picker isn't the only way to reach it.

## Search is scoped by the ticket's assignee

The picker opens on **your** tickets before you type a thing: one query per
ticket board, filtered on that board's own assignee property, freshest first
(`my_tasks`). Only boards that are actually *queried* count against the
fan-out cap — capping the raw board list instead spends the budget on boards
with no assignee column at all, which with 27 connected boards and 11 usable
ones left 8 of the 11 unread. The opening list is cached per person for 60s,
since it runs on focus and a dozen sequential board queries is a slow way to
open a dropdown twice. Typing searches every connected board by title (`search_tasks`,
through Notion's workspace-wide `search` — the right endpoint precisely because
the tickets are scattered across boards), keeping only results that are rows on
a known ticket board. That's what separates a ticket from a loose document that
happens to match. Yours sort first and are badged.

"Yours" is decided by `_assigned_to`, which matches:

- a **people** property against the asking person's Notion user id — the very id
  they logged in with, so no mapping table is needed; and
- an **email** or free-text property against their email address, for boards
  that don't use a people column.

Assignee and title properties are resolved **from each board's schema**, never by
a hardcoded name (`_task_schema`) — ticket boards belong to other teams and get
renamed freely. This is the same lesson as `alloc_person_prop`.

### Turning search on

It is off until the Hours Tracker integration can read the ticket boards. Notion
grants that per page, and **it inherits to every child**, so this is one
admin action, not per-user work:

> In Notion, open the top-level page (or teamspace root) holding the tickets →
> ••• → Connections → add **Hours Tracker**. Every board, sub-page and ticket
> underneath — including ones created next year — becomes searchable.

Until then `task_sources()` is empty, and the picker says so plainly instead of
looking broken: *"Ticket search needs a Notion board connected to Hours Tracker
— paste a ticket link instead."*

`TASKS_DS_IDS` (comma-separated data source ids) names the boards outright and
skips discovery. Worth setting for two reasons: discovery leans on Notion's
search index, which lags for freshly created databases, and naming the boards
keeps an unrelated database that someone connects later out of the picker.

**The privacy trade-off, stated plainly:** search runs with the *app's* view of
Notion, not the asking person's. Anyone who can log hours can therefore find and
link a ticket they personally can't open in Notion. Scoping by assignee is what
keeps the default list personal, but it is a convenience, not a permission
boundary. Making it a real one would mean per-user OAuth tokens — the app
already mints one at login and discards it (`web/auth.py:exchange_code`) — at
the cost of every person hand-picking pages on Notion's consent screen.

## Storage

Two plain properties on Time Entries, added on startup by
`ensure_task_properties`: **`Task URL`** (url) and **`Task`** (rich text, the
label). Deliberately *not* a relation: a Notion relation targets exactly one
data source, and znlove's tickets live across many boards.

Both are optional and read tolerantly (`entry_task`): every entry logged before
this feature has neither, and a rename in the Notion UI must not take a report
down. Nothing is written when there's no ticket, so CLI entries and Notion-form
submissions are unaffected.

## Where it shows up

- `/` — the picker, under Description. Enter chooses a ticket rather than
  submitting the form; the chosen ticket becomes a removable chip.
- `/project` — the ticket as a link in the entry's description cell (not its own
  column: the table's colspans already flex, and the column would be empty for
  every older entry).
- `/project.csv`, `/reports.csv` — `task` and `task_url` columns.
- `/project.xlsx` — a **Ticket** column in each sheet's log, hyperlinked.
- Google Sheet export — the same column as a `HYPERLINK()` formula.
- `/project/export` — read-only. Unlike the hours and the comment, the ticket is
  what the work was filed against, so it isn't the sender's to reword.

**Not on `/week`.** A grid cell is an upsert keyed on (person, project, date)
and can aggregate several sittings, so it has no single ticket. That needs
multi-entry cells designed first.

## Verified

- `parse_task_url` against 13 URL shapes, including the `app.notion.com` form
  Notion returns today, hex-ending slugs, side-peek links, board links and junk.
- `_assigned_to` against people / email / free-text assignee columns, dashed and
  dashless ids, and the mine-first ordering.
- End to end against real Notion: a throwaway ticket board (created, exercised,
  trashed) proved discovery, the assignee-scoped list, title search and
  `resolve_task`; then a real entry posted through the form arrived in Notion
  with its ticket and came back out through `/project`, the CSV and the workbook
  (with a live hyperlink). A junk link was refused and filed nothing.
