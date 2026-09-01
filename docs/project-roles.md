# Project roles: PM and Account manager

Every project can carry two owners — a **PM** and an **Account manager** — curated in
Notion, shown across the web app, and usable as a filter on **By project**, **Budgets**
and **Reports**. See `docs/project-roles-plan.md` for the original design; this is what
actually shipped.

The question these answer is "who do I chase about this project" — for an overrun, a
client email, a report someone disagrees with. That's why the filter lands on exactly the
three pages where that question gets asked, and why it's the same question `/budgets`
already reads to catch overruns.

## Where it lives

Two **people** properties on the Projects data source, added by `ensure_role_properties()`
at startup (`web/notion_ops.py`) — the same shape as `ensure_budget_properties`: read the
schema, add only what's missing, one update, safe on every boot.

| Property | Type |
| --- | --- |
| `PM` | people |
| `Account manager` | people |

People, not a select or text column, for the same reason every other person in this app is
a Notion user id: the roster (`list_people`), access control, allocations and assignments
all match on user id, so a role joins straight into what already exists — names resolve
through the roster, never trusted off the payload (people properties come back as bare
`{"object": "user", "id": …}` refs with no name — the same trap every other people property
in this codebase already works around).

Notion has no single-person property type, so both columns are arrays the app treats as
holding **at most one** person: `_role_from_props` reads the first entry, `set_project_role`
replaces the whole array on write. A second person added by hand in Notion is silently
ignored — the app never errors on it, it just never reads past the first.

Both are optional. A project with no PM reads as "—" everywhere and is excluded by a PM
filter, but is never hidden just for lacking one.

**Every read goes through `.get()`** (`_role_from_props`). This app has been taken down
twice by a renamed Notion column (`alloc_person_prop`; Time Entries' `Logged by`, currently
called `melisa`). A renamed role column reads as **"no PM"** — the project just stops being
filterable by it — never a 500.

Both ids ride along on the same `list_projects()` read the budget already parses off each
row, so neither role costs an extra Notion call anywhere it's used.

## Writing

    ops.set_project_role(project_id, role, person_id)   # role: "pm" | "am"; person_id=None clears it

One `pages.update`. Rejects an unknown `role` and a `person_id` that isn't on the roster —
both ids arrive from the browser, the same posture `set_entry_hours` takes refusing a page
outside its data source. The project's `People` property is never touched: a PM who never
logs hours on the project is normal, and membership stays the separate, explicit thing
`/assignments`' checkboxes already are.

`POST /api/project/role` (`web/app.py`) is the only way in — logged in, admin, same-origin,
exactly like `/api/assignment` and `/api/budget`. It saves one field per call, the same
reason `/api/budget` does: the first sitting over a project list is dozens of rows, and one
bad save shouldn't cost the rest.

## Editing: `/assignments` only

`/assignments` already owns "who is on which project"; each row grows two `<select>`s (PM,
Account manager) listing the whole roster plus a blank "—", saved on `change` via
`/api/project/role`. The select tracks its own last-saved value in `dataset.prev` so a
failed save reverts the dropdown instead of leaving a value on screen that was never
written — the same optimistic-save-then-revert idiom the assignment checkboxes next to it
already use.

Every other page shows the roles **read-only**:

- `/project`, one project selected — PM and Account manager as chips beside the name,
  resolved to `pm_name`/`am_name` in the route (not in the template) so a role id that's
  since dropped off the roster reads as "—" instead of breaking a Jinja lookup.
- `/project`, the all-projects rollup — two columns (`PM`, `Account manager`) next to
  each project's row; blank under the person rows nested inside it, since a role is
  project-level, not per-person. Hidden below 560px, next to the other numeric columns
  that already don't fit a phone-width row — the roles are one tap away on `/assignments`
  or `/budgets`.
- `/budgets` — two columns beside the project name.

## Filtering

Two repeated query params, mirroring `?person=` and `?project=`:

    ?pm=<notion user id>   (repeatable)
    ?am=<notion user id>   (repeatable)

Semantics, chosen to read the way people say it out loud:

- no pick for a role = every project, including ones with nobody in that role
- several picks within a role are **OR** ("Ana's or Beto's projects")
- the two roles combine with **AND** ("Ana's projects where Beto is the account manager")
- unknown ids are dropped, the `_project_picks` rule — a stale bookmark degrades to the
  unfiltered page rather than to an empty one

Two small helpers in `web/app.py` do the resolving, next to `_project_picks` they mirror:

    _role_picks(people, pm, am) -> (pm_ids, am_ids)         # sets; unknown ids dropped
    _role_match(project, pm_ids, am_ids) -> bool             # OR within a role, AND across
    _role_keep_ids(projects, pm_ids, am_ids) -> set | None   # None means "no filter"

A shared `web/templates/_role_filter.html` renders both dropdowns — the third twin of
`_people_filter.html` / `_project_filter.html`, same `.pfilter` markup and CSS classes so
the three filter rows can't visually drift apart, with its own `pmfilter`/`amfilter` DOM
ids and `rf*`-namespaced JS so it can sit on the same page as the other two. Unlike those
two partials it wraps its markup in a macro, since it renders the same dropdown twice —
`people` is passed into the macro explicitly rather than relied on implicitly, since a
Jinja macro doesn't inherit the include's context the way top-level template code does.

### The three pages

**`/project`** — `projects` is narrowed by role **before** `_project_picks` resolves
`?project=` against it (in `_project_role_scope`, shared with the CSV/Excel/export
routes), so the two filters compose: an explicit project pick is validated against the
already role-narrowed list, and a stale project id that fails the role filter degrades to
the rollup exactly like an unknown id does. When there's no explicit `?project=` pick, the
rollup's `keep_ids` falls back to the role filter's id set (`_role_keep_ids`) instead of
showing every project — otherwise a PM filter with no project pick would narrow the picker
but not the numbers on screen. The rollup is already one `entries_between` grouped in
Python, so this costs no extra Notion round trip. The picks ride the same `qs_*` pattern
`?project=` already does, into `/project.csv`, `/project.xlsx` and `/project/export`, so an
export matches the screen it was launched from.

**`/budgets`** (+ `/budgets.csv`) — narrows `projects` the same way, before `_project_picks`
and before `_budget_rows`. This is the page where "who owns this overrun" gets asked, so the
role columns are also *displayed* here.

**`/reports`** (+ `/reports.csv`) — entry-centric, so the filter resolves to a set of
project ids (`_role_keep_ids` over `list_projects(active_only=False)`, so a role pick still
narrows entries logged against a project that's since gone inactive) and narrows the
entries **and** the planned rows in memory, next to where the people pick already applies —
`planned_rows()` grew a `project_id` field for exactly this, alongside the `project` name it
already carried, mirroring how `entries_between` carries both. Unlike the people pick this
filter is not admin-only in `_report_data` — the whole `/reports` page happens to be
admin-gated anyway, but the narrowing logic doesn't rely on that: it only ever removes rows
from whatever scope the viewer already has.

## Tests

`./.venv/bin/python tests/test_project_roles.py` — plain asserts, no pytest, no Notion
calls. Covers `_role_from_props` parsing (missing column, a renamed one, two people in the
column), `_role_picks`'s roster-validation and set semantics, `_role_match`'s OR-within /
AND-across / no-pick-matches-everything rules, `set_project_role`'s two refusals (unknown
role, off-roster person), and the `/reports` narrowing end to end (a role pick drops
entries and planned rows on the excluded project; no pick leaves everything in place).

## Not built

No bulk assignment (an admin sets one project's role at a time, the same granularity as a
budget edit), no history of who held a role when, and no notion of "co-PM" — the column is
one person or nobody, by design, the way every other single-owner idea in this app already
works.
