# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A time tracker where **Notion is the database / source of truth**. Two Notion databases (Projects, Time Entries — plus an optional Allocations one for the schedule/forecast view) live under a "Hours Tracker" page. On top of that:

- `web/` — FastAPI + Jinja2 + HTMX web app (log hours form, editable Mon–Fri weekly grid, reports with CSV export, per-project hours by month/week/day, schedule/allocations grid, start/stop timer). Deployed on Render free tier (`render.yaml`), live at hours-znlove.onrender.com.
- `src/` — Python CLI scripts: one-time schema setup, project seeding, backfill logging, reports.

There are no tests and no linter configured.

## Commands

```bash
# setup
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# run the web app locally (AUTH_DISABLED=1 in .env bypasses Notion OAuth — local dev only)
./.venv/bin/uvicorn web.app:app --reload

# CLI scripts (all read .env + databases.json via src/config.py)
./.venv/bin/python src/setup_databases.py          # one-time: creates the Notion DBs, writes databases.json
./.venv/bin/python src/setup_people_db.py          # creates + seeds the People roster db (idempotent; re-run to add new members)
./.venv/bin/python src/seed_projects.py "Name" …   # dedupe-safe bulk add
./.venv/bin/python src/log_hours.py --project X --hours 2.5 --desc "..."
./.venv/bin/python src/report.py [--by project] [--since YYYY-MM-DD]
```

## Architecture

- **Notion 2025-09-03 API** (notion-client ≥ 3): every database wraps a *data source*. Schema lives on the data source, new pages parent to `data_source_id`, queries go through `notion.data_sources.query`. Keep using this style — don't fall back to the older `databases.query` API.
- **ID resolution** (`src/config.py:load_db_ids`): locally, ids come from `databases.json` (gitignored, written by `setup_databases.py`); on Render they come from env vars (`PROJECTS_DS_ID`, `TIME_ENTRIES_DS_ID`, `ALLOCATIONS_DS_ID`, `PEOPLE_DS_ID`, plus `*_DB_ID`). Env vars override the file. New databases must be wired into both paths.
- **People roster** (`list_people` in `web/notion_ops.py`): the people shown everywhere (assignments columns, schedule rows, dropdowns) come from the **People** database (curated in Notion: untick `Active` to hide — don't delete the row, re-running the seeder would re-add them; row title renames; `Person` link is required). When `people_ds_id` isn't configured — or the roster query fails (e.g. bad `PEOPLE_DS_ID`) — it logs and falls back to listing all workspace members rather than 500ing. New workspace members don't appear until `setup_people_db.py` is re-run or a row is added by hand.
- **Access control lives in the People db** (`access_ids` in `web/notion_ops.py`, consumed by `auth.is_allowed`/`is_admin`): who may log in and who is admin is curated in Notion, matched by the linked `Person`'s Notion user id (the same id OAuth returns) — **not** by email. `Active` = can log in; the `Admin` checkbox = team-wide reports/exports scope. The derived id sets are TTL-cached (~60s) since `is_admin` runs several times per request, so Notion edits take up to a minute to take effect. `is_allowed` is re-checked on every request too (via `_require_login` in `app.py`, reusing the cached sets), so unticking `Active` ends a live session within the TTL — not just future logins. `ALLOWED_EMAILS` / `ADMIN_EMAILS` env vars remain a **fallback** (OR'd in) so a People-db misconfig can't lock everyone out; on query failure the check degrades to env-only. Add a person by giving them an Active People row (`setup_people_db.py` or by hand); revoke by unticking Active.
- **`web/` imports from `src/`**: `web/notion_ops.py` does `sys.path.insert` to reuse `src/config.py`'s client and id loading. All Notion reads/writes for the web app live in `notion_ops.py`; `web/app.py` holds only routes/HTTP concerns.
- **Auth** (`web/auth.py`): Notion OAuth is used for *identity only* — the authorizing user's identity is checked against the People-db roster (Active row = allowed, Admin tick = admin; see "Access control" above), with `ALLOWED_EMAILS`/`ADMIN_EMAILS` as a fallback. All data access then uses the internal integration token (`NOTION_TOKEN`). `is_allowed`/`is_admin` take the **user dict** (`{id, email}`), not a bare email, so they can match by Notion id. Mutating routes check same-origin (`_same_origin` in `app.py`) as CSRF protection.
- **Who logged an entry**: web-app entries write an explicit `Person` (people) property (re-added on startup by `ensure_person_property` if missing); Notion-form submissions rely on the auto-filled `Logged by` (created-by) property; CLI entries have no human submitter. Readers (`report.py`, the weekly grid) must handle both `Person` and `Logged by`.
- **Weekly grid** (`/week`): cell edits are upserts keyed on (person, project, date); hours = 0 deletes the entry.
- **Schedule / allocations are day-first** (`/schedule`, admins): every allocation row carries a real weekday date (the Notion property is still named `Week`). There is no week-scope write — `ops.set_allocation_range` upserts one row per weekday in a range and skips weekends, taking `_write_lock` once for the whole range; `hours = 0` deletes those days. The **Days** view is the planner: one row per person (or per project), each day cell a stack of project pills, so several projects can share a day; a click opens the assign popover (project + hours/day + "repeat through"). The **Weeks** view is a read-only six-week rollup that buckets each day into its Monday. Capacity is `DAY_TARGET_HOURS` (default 8; `WEEK_TARGET_HOURS` still overrides the weeks rollup's number). Booking someone onto a project also adds them to its `People` property, so `/schedule` and `/assignments` can't drift. Allocations written before the day-first planner sit on a Monday and simply read as hours on that Monday.
- **People properties come back nameless**: Notion returns `{"object": "user", "id": …}` with no name for people properties, so anything showing a person's name must resolve it against the roster (`_person_name_map` in `notion_ops.py`, or the `people` list already loaded by a route) rather than trusting the payload.
- **Reports people filter**: `/reports` (and `/reports.csv`) accept repeated `?person=<notion user id>`. `_report_data` filters entries *and* allocations in memory (the Notion query stays unfiltered) and a non-empty pick implies team scope — so the pick is honored for admins only, and ignored for everyone else. Cross-breakdowns (`_person_projects`, `_person_project_matrix`) are derived from the same filtered entries; both sort by hours desc, tie-broken by name.
- **Per-project view** (`/project`, admins): **one** period (a month, a week or a day — not a window of several), either rolled up across all projects or drilled into one. `_period_range` in `web/app.py` owns all three granularities: it returns the date bounds, the label, the prev/now/next anchors and which native picker to show (`date` vs `month`), so adding a granularity is one branch there. `?start=` is the anchor — `_project_anchor` widens the month picker's `YYYY-MM` to the 1st before parsing. `?project=` is a project id, or `all`/unset for the rollup (the default). The two modes read differently on purpose: one project goes through `ops.project_entries` (filters on the Project relation *in the Notion query*), while All uses a single `entries_between` for the period and groups by `project_id` in Python — one read for every project instead of one query each.
- **No Notion views via API**: Form and reporting views can only be created in the Notion UI, never programmatically.

## Deploy

Push to `main` → Render auto-deploys (blueprint in `render.yaml`, start command `uvicorn web.app:app`). Secrets are set in the Render dashboard, not in the repo. A launchd job on this machine (`com.jp.hours-keepalive`) pings `/healthz` every 10 min to keep the free instance warm.
