# Custom date range on /project, and archived projects

## Custom range (`?period=custom&start=…&end=…`)

`/project` (By project) had three granularities — Daily, Weekly, Monthly — each
one period anchored on `?start=`. **Custom** is a fourth: any from–to range.

- `_project_range(period, start, end)` in `web/app.py` is the one entry point for
  the page, `/project.csv`, `/project.xlsx`, `/project/export` and
  `POST /api/entry/goal`, so a link can't be read two ways. The three old
  periods still go through `_period_range`; `custom` goes through
  `_custom_range`, which returns the same dict shape (`from`, `to`, `label`,
  `prev`/`next`/`now`, plus `*_end` for each).
- Missing bounds = month-to-date. Reversed bounds are swapped. A range longer
  than `_MAX_CUSTOM_DAYS` (366) is clipped at the far end — it's one
  `entries_between` read, so it stays bounded.
- ← / → step by the range's own length; the middle button ("To today") is the
  same length ending today.
- The template carries `&end=` on every link (`qse`) only when the period is
  custom; the export page's back links do the same.
- Invoice stays monthly-only (`can_invoice` checks `period == "monthly"`), and
  goal target meters still only draw on a monthly period.

`/reports` already had a custom range (`range=custom&date_from=…&date_to=…`).

## Archiving a project

Archive = untick the project's `Active` checkbox in Notion. The app can now do
it: **Project owners** (`/projects/roles`) has an Archive / Restore button per
row and an **Archived** tab, via `POST /api/project/active` →
`ops.set_project_active` (admin + same-origin; checks the page's parent is the
Projects data source before writing).

What archiving does and doesn't do:

- It **does** drop the project from everything that lists active projects —
  the log-hours form, the weekly grid, the schedule planner, budgets.
- It **doesn't** touch any entry, allocation, invoice or goal.
- Reporting keeps the history: `_project_scope` (all `/project*` routes) and
  `/reports` now read `list_projects(active_only=False)`. The shared project
  picker (`_project_filter.html`) shows archived projects last, under an
  **Archived** heading, so they can be picked and exported.
- The all-projects rollup lists an archived project only when it has hours in
  the period (or it was picked) — a zeroed row for every finished project would
  bury the live ones. Active projects are still zero-filled as before.

Tests: `tests/test_project_range.py` (no Notion).
