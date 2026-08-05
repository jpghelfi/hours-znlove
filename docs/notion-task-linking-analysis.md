# Linking a Notion task to a tracked hour entry — analysis

*Written 2026-08-05. Design note, nothing built yet.*

## What's being asked

When logging hours, optionally attach the Notion ticket the work was for — by pasting a
ticket URL, or by searching for it. The link then travels with the entry: visible in the
reports and in the client-facing export.

Tickets at znlove are **not in one database** — they're spread across many pages and
boards in the workspace. That single fact drives most of what follows.

## The phases

| | What the user gets | What it needs from Notion | Effort |
|---|---|---|---|
| **1 — paste a link** | Paste a ticket URL on the log form; it's stored, shown, exported | **Nothing. No sharing, no admin, no re-auth.** | ~130 lines |
| **2 — search** | A search box that finds tickets by name, real titles, links verified | The integration added to the top-level pages / teamspaces (admin, one-off) | ~150 more |
| **3 — per-user scope** | Search shows each person only what *they* can see in Notion | Each user re-authorizes and hand-picks pages | ~60 more, high friction |

Phase 1 first is the right call, and for a better reason than "it's smaller": it is the
only version that needs **no permission grant at all**, so it can't be blocked, can't
half-work when a board is missed, and never shows anyone a "that page isn't shared" error.

## Phase 1 — paste a link (no Notion permissions involved)

The trick is that **a Notion URL already contains the title**. Everything needed is in
the string the user pastes; the app never has to read the page:

```
https://www.notion.so/znlove/Fix-checkout-race-condition-3b101234695c81d5a9ebd56e145122f4?pvs=4
                             └────── title slug ──────┘ └─────── page id (32 hex) ──────┘
```

Parsed (verified against real URL shapes):

| Pasted | id | label |
|---|---|---|
| `…/Fix-checkout-race-condition-3b10…f4?pvs=4` | `3b10…f4` | `Fix checkout race condition` |
| `…/Ticket-with-ñ-and-spaces-3b10…f4#4a1b2c3d` | `3b10…f4` | `Ticket with ñ and spaces` (block fragment ignored) |
| `…/3b101234695c81d5a9ebd56e145122f4` | `3b10…f4` | *(none — user types one)* |
| `…/Sprint-board-3970…18?v=<view>` | — | rejected: `?v=` means a board, not a ticket |
| `https://example.com/nope` | — | rejected: not a Notion URL |

Two parsing gotchas worth writing down now, both found by testing:

- Anchor the id at the **end** of the last path segment (`([0-9a-f]{32})$` after stripping
  dashes). Searching the segment loosely mis-slices the id by a character when the slug
  itself ends in hex-ish text.
- A side-peek link (`…/Tickets-board?p=<ticket id>`) carries the *board's* slug in the
  path, not the ticket's — so take the id from `?p=` and leave the label blank rather
  than labelling the ticket with its board's name.

**Storage:** two plain properties on Time Entries — `Task URL` (url) and `Task`
(rich text, the label). No relation (a relation targets exactly one database, and znlove's
tickets aren't in one). The label is editable in the picker, so a bare-id link still gets a
human name, and a Notion rename doesn't need the app to notice.

**UI:** under Description on `/entry`, an optional "Link a Notion task" field — paste,
and it resolves to a removable chip. `POST /entry` re-parses server-side (never trusts
what the browser sends) and rejects anything that isn't a notion.so page URL.

**What phase 1 does not give you:** no verification the ticket exists, no live title, no
hours-per-ticket rollup inside Notion. The label is a snapshot taken at logging time.

## Phase 2 — search across the workspace

Search needs the app to actually *read* tickets, and today it can't: the internal
integration sees exactly four data sources — everything under the Hours Tracker page and
nothing else.

```
Allocations   ·   Time Entries   ·   Projects   ·   People
```

### How to grant it for everyone at once

Notion access for an integration is granted **per page, and inherits to every child** —
so this is not per-user work, and it does not have to be repeated for new tickets:

1. A workspace admin opens each **top-level page / teamspace root** that holds tickets.
2. ••• → Connections → add **Hours Tracker**.
3. Every board, sub-page and ticket underneath — including ones created next year — is
   readable from then on.

That's a handful of clicks, once, covering everyone. (Workspace admins separately control
which connections are *allowed* to exist at all, in Settings → Connections; that approval
is not the same as page access — the add-to-page step above is what grants reading.)

The consequence to accept deliberately: the app then searches with **its own** uniform
view, not the asking person's. Anyone who can log hours could find and link a ticket they
personally can't open in Notion. Inside one company that's normally fine — but it should
be a conscious yes, and it's exactly what phase 3 exists to undo.

### How the search works

With tickets spread across many databases, Notion's `search` endpoint is the right tool
(it's workspace-wide and matches titles): `search(query=q, filter={object: page})`, then
in Python keep the pages whose parent is a `data_source` — that's what separates database
rows (tickets) from loose documents — sort by `last_edited_time`, take ~10.

`GET /api/tasks/search?q=…`, HTMX-debounced at 300 ms and 2 characters minimum, rendering
the same little `<ul>` the existing filter partials use. One round trip, ~300–600 ms.

Phase 2 also upgrades phase 1's links for free: a pasted URL can now be resolved through
`pages.retrieve` to confirm it exists and pull the *real* title, and the stored label can
be refreshed on read — the "sync" half of the ask. Old phase-1 entries keep working
untouched; they just start showing verified titles.

## Phase 3 — per-user permissions (only if it's a hard requirement)

The app already runs a full Notion OAuth dance at login and **throws the access token
away** after reading the person's identity (`web/auth.py:exchange_code`). Keeping that
token in the session and searching with it would give true per-user scope — no new
storage, it dies with the session, ~60 lines.

The catch is Notion's consent screen: the user **hand-picks which pages** to grant, so
each person's search only covers what they selected, and widening it means re-authorizing.
That friction is why this is phase 3 and not phase 2 — worth it only if "one person must
not see another's tickets" is a real requirement rather than a preference.

## Code shape (both phases)

- `web/notion_ops.py` — `ensure_task_properties()` beside the existing
  `ensure_person_property` / `ensure_admin_property` startup hooks; `create_entry()` grows
  optional `task_url` / `task_label`; `entries_between` / `project_entries` return them.
  Read the new properties **tolerantly** — a rename in the Notion UI has taken pages down
  here before (the `val` incident in CLAUDE.md; and note `Logged by` on Time Entries is
  *currently* renamed to `melisa`, so Notion-form entries are already losing their
  submitter). Phase 2 adds `search_tasks(q)` and `resolve_task(url)`.
- `web/app.py` — task fields on `POST /entry` (re-parsed server-side), plus
  `GET /api/tasks/search` in phase 2.
- Templates — `_task_picker.html` included by `form.html`; a ticket link column in
  `project.html` and `project_export.html`.
- `web/report_xlsx.py`, `report_gsheet.py`, the CSV route — one extra column each.
- **Not `/week`.** A grid cell is an upsert keyed on (person, project, date) and can
  aggregate several sittings, so it has no single ticket. Adding it there means designing
  multi-entry cells first — a separate piece of work.

No migration: entries logged before this simply have no task, and every reader must treat
the property as optional forever.

## Open questions

1. Phase 1 now, phase 2 after someone shares the teamspace roots — agreed? (Assumed yes.)
2. Should the picker also offer a free-text ticket label with no URL at all (for tickets
   that live in Jira/Linear/somewhere else)? Cheap to add in phase 1, easy to regret.
3. Ticket on the weekly grid eventually, or is the log form enough?
