# Plan: a PM and an Account manager on every project

Every project gets two owners — a **PM** and an **Account manager** — curated in
the Notion Projects table (the same row that already carries `Active`, `Client`,
`People` and the budget columns), shown in the web UI, and usable as a filter on
**By project**, **Budgets** and **Reports**.

The question these answer is "who do I chase about this project" — for an
overrun, for a client email, for a report someone disagrees with. That is why
the filter lands on exactly the three pages where that question gets asked.

## Data model

Two **people** properties on the Projects data source:

    PM                 people
    Account manager    people

People, not a select or a text column, for the same reason every other person in
this app is a Notion user id: the roster (`list_people`), access control, the
allocations column and the assignments page all match on user id, so a people
property joins straight into what already exists — names resolve through
`_person_name_map`, and Notion's own table shows a real avatar next to the name
(what the Projects table already does for `People`).

Notion has no single-person property type, so both columns are arrays that the
app treats as holding **at most one** person: readers take the first entry,
writers replace the whole array. A second person added by hand in Notion is
ignored by the app rather than erroring.

Both are **optional**. A project with no PM reads as "—" everywhere and is
matched by no PM filter (but is never hidden unless a filter is on).

`ensure_role_properties()` adds whichever column is missing on boot — the same
shape as `ensure_person_property` / `ensure_task_properties` /
`ensure_budget_properties`: retrieve the schema, add only what isn't there, one
update, safe to run on every start. No migration script.

Every read goes through `.get()`, like `_budget_from_props`: this app has been
taken down twice by someone renaming a Notion column (`alloc_person_prop`, the
`Logged by` column that is currently called `melisa`). A renamed role column must
read as "no PM" — the project simply stops being filterable — never as a 500.

## Reads

`list_projects` already pages every project row for the budget; the roles are
parsed off those same rows, so they cost **no extra Notion call anywhere**:

    project["pm_id"], project["am_id"]      # Notion user id or None

Names are **not** taken from the payload — people properties come back as bare
`{"object": "user", "id": …}` refs with no name — so anything printing a name
resolves it against the roster the route has already loaded (`list_people()`), or
`_person_name_map()`.

## Write

    ops.set_project_role(project_id, role, person_id | None)   # role in ("pm", "am")

One `pages.update`, `None` clears the column. Rejects an unknown role and a
person who isn't on the roster (a client-supplied id, same posture as
`set_entry_hours` refusing a page outside its data source).

Endpoint: `POST /api/project/role` — logged in, **admin**, same-origin, exactly
like `/api/assignment` and `/api/budget`. Saves one field per call, because the
first sitting is 37 rows of dropdowns and one bad save shouldn't cost the rest.

Setting a role does **not** add the person to the project's `People` property. A
PM who never logs hours on the project is normal; membership stays the separate,
explicit thing it is today.

## Where roles are edited

`/assignments` — the page that already owns "who is on which project". Each
project row grows two `<select>`s (PM, Account manager) listing the whole roster
plus a blank "—", saved on `change` via `/api/project/role` with the page's
existing optimistic-save + revert-on-failure idiom.

Nowhere else edits them. Every other page shows them read-only.

## Filtering

Two new repeated query params, mirroring `?person=` and `?project=`:

    ?pm=<notion user id>   (repeatable)
    ?am=<notion user id>   (repeatable)

Semantics, chosen to read the way people say it out loud:

* no pick for a role = every project (including projects with nobody in that role)
* several picks within a role are **OR** ("Ana's or Beto's projects")
* the two roles are **AND** ("Ana's projects where Beto is the account manager")
* unknown ids are dropped, so a stale bookmark degrades to the unfiltered page
  rather than to an empty one — the `_project_picks` rule

Server side, one helper resolves and one applies:

    _role_picks(people, pm, am) -> (pm_ids, am_ids)     # sets, unknown ids dropped
    _role_match(project, pm_ids, am_ids) -> bool

### The three pages

**`/project`** (By project) — narrow `projects` by role **before**
`_project_picks` and before the rollup. The rollup is already one
`entries_between` grouped in Python, so a role filter costs no extra round trip;
a single surviving project still drills in. The picks ride along on every
period/nav link and into `/project.csv`, `/project.xlsx` and `/project/export`
through the same `qs_*` loop the person picks use, so an export matches the
screen it was launched from.

**`/budgets`** (+ `/budgets.csv`) — narrow `shown` the same way, before
`_budget_rows`. This is the page where "who owns this overrun" is asked, so the
role columns are also *displayed* here beside the project name.

**`/reports`** (+ `/reports.csv`) — entry-centric, so the filter resolves to a
set of project ids and narrows the entries **and** the planned rows in memory,
next to where the people pick is already applied. It narrows whatever scope the
viewer already has (it only ever removes rows), so unlike the people pick it is
not admin-only.

A shared `web/templates/_role_filter.html` partial renders both dropdowns — the
third twin of `_people_filter.html` / `_project_filter.html`, same `.pfilter`
markup and styles, so the three filter rows can't drift apart. It takes
`people`, `pm_selected`, `am_selected`.

## Display

* `/project`, one project selected — PM and Account manager as two chips in the
  page header, beside the project name.
* `/project`, the all-projects rollup — two columns in the project table.
* `/budgets` — two columns beside the project name.
* `/assignments` — the editable dropdowns above.
* `/reports` — no per-project columns (it aggregates by project *name*); the
  filter row is enough.

Everywhere, an unset role prints a muted "—", never a blank cell.

## Tests

`tests/test_project_roles.py`, in the existing plain-assert style (no pytest, no
Notion calls — monkeypatch anything that would make one):

* parsing: missing column → `None`; renamed column → `None`, not a raise;
  two people in the column → the first
* `_role_picks` drops unknown ids and keeps the order-independent set semantics
* `_role_match`: OR within a role, AND across roles, no-pick matches everything,
  a project with no PM is excluded by a PM pick but kept when no pick is on
* `set_project_role` refuses an unknown role and an off-roster person
* the reports narrowing: entries on a project outside the role pick are dropped

## Docs

`docs/project-roles.md` (how it works and why these choices), plus one bullet in
`CLAUDE.md`'s architecture list in the voice of the ones around it.
