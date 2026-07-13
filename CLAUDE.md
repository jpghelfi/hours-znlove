# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A time tracker where **Notion is the database / source of truth**. Two Notion databases (Projects, Time Entries — plus an optional Allocations one for the schedule/forecast view) live under a "Hours Tracker" page. On top of that:

- `web/` — FastAPI + Jinja2 + HTMX web app (log hours form, editable Mon–Fri weekly grid, reports with CSV export, schedule/allocations grid, start/stop timer). Deployed on Render free tier (`render.yaml`), live at hours-znlove.onrender.com.
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
- **`web/` imports from `src/`**: `web/notion_ops.py` does `sys.path.insert` to reuse `src/config.py`'s client and id loading. All Notion reads/writes for the web app live in `notion_ops.py`; `web/app.py` holds only routes/HTTP concerns.
- **Auth** (`web/auth.py`): Notion OAuth is used for *identity only* — the authorizing user's email is checked against `ALLOWED_EMAILS`, then all data access uses the internal integration token (`NOTION_TOKEN`). `ADMIN_EMAILS` gates the team-wide reports scope. Mutating routes check same-origin (`_same_origin` in `app.py`) as CSRF protection.
- **Who logged an entry**: web-app entries write an explicit `Person` (people) property (re-added on startup by `ensure_person_property` if missing); Notion-form submissions rely on the auto-filled `Logged by` (created-by) property; CLI entries have no human submitter. Readers (`report.py`, the weekly grid) must handle both `Person` and `Logged by`.
- **Weekly grid & allocations**: cell edits are upserts keyed on (person, project, date/week); hours = 0 deletes the entry. Allocation writes are serialized with a lock (`_set_allocation_locked`) to avoid duplicate rows from concurrent upserts. Allocation rows carry real dates (the property is still named `Week`): the Schedule's Days view upserts exact days (`scope="day"`), a Weeks-view cell edit replaces the pair's whole week with one Monday-dated row (`scope="week"`), and the weeks grid buckets any date into its Monday column.
- **No Notion views via API**: Form and reporting views can only be created in the Notion UI, never programmatically.

## Deploy

Push to `main` → Render auto-deploys (blueprint in `render.yaml`, start command `uvicorn web.app:app`). Secrets are set in the Render dashboard, not in the repo. A launchd job on this machine (`com.jp.hours-keepalive`) pings `/healthz` every 10 min to keep the free instance warm.
