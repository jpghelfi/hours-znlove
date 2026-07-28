# Day editor — several entries in one grid cell

## The problem it solves

The weekly grid is one number per (person, project, day). Real days aren't always
one thing. Pablo logged Auter on Jul 22 as two entries:

| hours | description |
|---|---|
| 1 | Create new repo, connect new theme to main branch |
| 1 | ZN-1037 Button for images |

The grid summed those into a single `2`. Editing that cell to `3` used to run:

```python
keep = matches[0] if matches else None
for extra in matches[1:]:      # duplicates from old races/forms: fold into one
    _notion.pages.update(extra["id"], archived=True)
```

…which archived the "ZN-1037" row — **description gone, no warning**. The code
couldn't tell an accidental duplicate from a deliberate split, so it treated both
as corruption. Four such pairs existed in live data when this was found.

## The rule

Rows on the same (person, project, date) are grouped by **normalized description**
(whitespace-collapsed, lowercased):

- **Same text** (including two blank ones) → a genuine duplicate: a double submit,
  an old race. Safe to fold into the oldest row, because the incoming `hours` is an
  authoritative new total for that day.
- **Different text** → different tasks. One number can't say which one changed, so
  `set_cell` **writes nothing and archives nothing**, returning:

  ```json
  {"ok": false, "code": "multi_entry", "hours": 2, "entries": [{"id": …, "hours": 1, "description": …}, …]}
  ```

The same guard covers deletion: blanking a two-task cell used to archive both rows
behind a single "Remove this entry?" confirm. Now it refuses and opens the editor.

## The UI

`week_grid` rows carry `counts` next to `cells`, so the template knows a cell sums
more than one entry and badges it `2×`.

- **1 entry** → inline typing, exactly as before. The fast daily path doesn't change.
- **2+ entries** → focusing the cell opens the day editor instead of editing a number.
- **`⋯` on any cell** (revealed on hover / focus-within) opens the editor too. This is
  how you *create* a split — without it the editor could only ever repair days that
  were already split, and there'd be no way to make one.

The editor lists one line per entry (hours + description), each saving on change, plus
**＋ Add line** and a per-line remove with confirmation. Closing it pushes the new total
and entry count back onto the grid cell and recalculates the row/day/week totals.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /api/day?project_id=&date=` | the entries behind one cell |
| `POST /api/entry/add` | `{project_id, date, hours, description}` → new entry |
| `POST /api/entry/update` | `{id, hours, description}`; `hours: 0` removes it |
| `POST /api/entry/delete` | `{id}` |

All four require login, same-origin, and a known Notion identity. Writes always use the
**logged-in** person — a client-supplied `person_id` is ignored, matching `/api/cell`.

### Trusting ids

`_owned_entry` re-fetches any entry id that arrives from the browser and rejects it
unless *both*:

1. `parent.data_source_id` is the Time Entries data source (so a Projects or Allocations
   page id can't be edited through these routes), and
2. the row's `Person` is the caller.

Otherwise a guessed id would let anyone edit someone else's hours. Verified live against
a real entry owned by another person and against a Projects row — both rejected.

## Gotcha: `sorts` on timestamps is silently ignored

"Fold into the oldest row" needs a deterministic order. The obvious query sort:

```python
"sorts": [{"timestamp": "created_time", "direction": "ascending"}]
```

is **accepted and ignored** by the 2025-09-03 data-source endpoint — ascending,
descending, and no-sort all returned identical order. Property sorts (`{"property":
"Hours"}`) do work; only the timestamp form is dropped. So `_day_rows` sorts in Python.

`created_time` is minute-granular (Notion truncates the seconds), so two entries created
in the same minute tie — id breaks it. Order is stable across reloads, which is what
"the oldest row wins" needs.

## Not covered here

`POST /entry` (the log-hours form) still has no double-submit guard. A double-click on a
slow cold start can create a real duplicate — one the fold logic above *would* then
correctly merge, but it's better not to create it. Fixing that (submit lock +
idempotency nonce) is separate work.
