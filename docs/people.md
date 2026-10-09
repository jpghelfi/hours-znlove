# People (`/people`)

The People database is both the roster every page shows and the access list
(`access_ids`): an **Active** row may sign in, **Admin** adds the team-wide
reports and admin pages, **Approves absences** adds the absence queue. Until
this page, the only ways to change it were `src/setup_people_db.py` and
hand-editing Notion. `/people` (admins only, under **Admin ▾** and in the
phone's More sheet) does it in the app.

## What it does

- **Lists every row, inactive included** (`ops.list_roster`, one query, active
  first then by name). `list_people` and `access_ids` only ever read Active
  rows; this page has to see the inactive ones because reactivating someone is
  half the reason to open it.
- **Three toggles per row** — Active, Admin, Approves absences — that autosave
  like `/assignments`' checkboxes (`POST /api/people/flag`) and snap back with
  the server's reason if the save fails. Unticking your **own** Active or Admin
  asks first (`zConfirm`), since it ends your access.
- **Inline rename** (`POST /api/people/rename`): the row title is the name the
  app shows everywhere. Whitespace is collapsed; blank is refused.
- **Add person** (`POST /api/people/add`): a picker of Notion **workspace
  members** (`users.list`, `type == "person"` — bots never, and guests aren't
  returned by that endpoint at all) who aren't on the roster. A new row gets the
  member's Notion name as its title, the `Person` link, and Active ticked — the
  same row `setup_people_db.py` would seed. The member list is the seeder's own
  `list_workspace_members`, reused rather than copied.

## Rules worth knowing

- **Dedupe is by Notion user id against every row, active or not**
  (`ops.add_candidates`, ids compared with dashes stripped). A member whose
  only row is inactive appears under **Inactive — reactivate**, and choosing
  them ticks that row's Active instead of creating a second row (two rows for
  one user is what `list_people` has to paper over). Anyone with an active row
  isn't offered at all. `add_person` re-checks on the server and refuses with a
  409 naming the existing row, which covers a second tab racing the first.
- **The last active admin can't be removed** (`ops.check_last_admin`, a 409).
  Unticking Admin *or* Active on the only remaining active admin would leave
  nobody able to open this page; `ADMIN_EMAILS` would be the only way back.
  "Admin" is counted the way `access_ids` counts it: another active, linked
  Admin row keeps one in place (even a duplicate row for the same person); an
  inactive row or a row with no linked user doesn't.
- **Every write checks the page is a People row** (`_people_page`): the page id
  comes from the browser, so it's retrieved and its parent data source compared
  against `PEOPLE_DS` before anything is written — the same posture as
  `set_entry_hours` and `get_invoice`. The add endpoint's user id is checked
  against the live workspace member list.
- **The access cache is dropped after every write** (`invalidate_access_cache`),
  so unticking someone's Active ends their session on their next request
  rather than up to a minute later. That's this process only: another worker,
  or an edit made directly in Notion, still waits out the ~60 s TTL.
- Writes share `_write_lock`; the guard reads the roster inside it, so two
  admins unticking each other at once can't both pass the last-admin check in
  one process.

## Not here

- No deleting rows — untick Active. Deleted rows would come back on the next
  `setup_people_db.py` run, and the row keeps the name on old entries.
- No people outside the Notion workspace: every row needs a `Person` link to
  sign in, be assigned or log hours.
- Rows with no linked Notion user are listed (flagged "no Notion user") and can
  be renamed or toggled, but can't sign in until someone links a Person in
  Notion.

## Tests

`tests/test_people.py` — the picker's dedupe, the last-admin guard, and the
write paths against a fake Notion client (no real People row is touched),
plus the page and endpoints through `TestClient`.
