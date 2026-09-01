# The /reports filter row

`/reports` narrows one period of logged hours four ways, all through the same
`<details>`-of-checkboxes markup (`.pfilter`) so the row reads as one control:

| Filter | Param | Partial | Means |
| --- | --- | --- | --- |
| People | `?person=` (repeats) | `_people_filter.html` | no pick = everyone |
| PM | `?pm=` (repeats) | `_role_filter.html` | no pick = every project |
| Account manager | `?am=` (repeats) | `_role_filter.html` | no pick = every project |
| Project | `?project=` (repeats) | `_project_filter.html` | no pick = every project |

The project picker is the same partial `/project` and `/budgets` use, included
with `{% with selected = r.project_selected %}` — one dropdown, so the two pages
can't drift apart in behavior or looks.

## How they compose

Every filter is applied **in memory** inside `_report_data`, against one
unfiltered `entries_between` read for the period plus one `planned_rows` read.
Narrowing therefore costs no extra Notion round trip, which is the reason none of
this is pushed into the Notion query.

- The people pick implies **team scope** (it's a team-wide read narrowed down),
  so it's honored for admins only. The project and role picks don't: they only
  ever *remove* rows from whatever scope the viewer already has.
- The project pick and the role pick are two ways of naming a set of project ids,
  so they intersect (**AND**): "Ana's projects" narrowed to the two of them you
  actually wanted to see. Within a filter, several picks are OR.
- The result (`keep_ids`) filters **entries and planned rows alike**, so the
  scheduled-vs-tracked cards can't quietly keep a project the tables dropped.
  `planned_rows()` carries `project_id` next to the project name for this, the
  way `entries_between` already did.
- `keep_ids is None` means "no project-side filter at all", kept distinct from
  an empty set — which legitimately means "filtered down to nothing".

## Resolving `?project=`

Through `_project_picks`, the same helper `/project` uses: repeats are
de-duplicated in query order, unknown ids are dropped (a stale bookmark degrades
to the unfiltered page rather than to an empty one), and the legacy `all`
sentinel reads as no pick.

Ids resolve against `list_projects(active_only=False)`, because an old entry's
project may have been unticked in Notion since — a filter shouldn't lose its
hours. The **dropdown** still offers only active projects, plus any archived one
that's actually picked, so a link to it keeps working and keeps its name. The
route reads that list once and hands it to `_report_data` (`project_list=`),
which would otherwise read it again; `/reports.csv` passes nothing and the read
happens lazily, only when something was actually picked.

Picks ride along on every range button and the CSV link via the `jsq` loop in
`reports.html`, so a filter survives navigation and the export matches the screen
it was launched from.

## Tests

`./.venv/bin/python tests/test_report_project_filter.py` — plain asserts, no
pytest, no Notion calls. Covers no pick / one / several, an archived project, a
stale id, the `all` sentinel, duplicate collapse, composition with the role and
people picks (including a disjoint pick that empties the page), and that a
caller-supplied project list skips the second read.
