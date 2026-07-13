# hours-znlove — Notion hours tracker

A time tracker backed by Notion. **Notion is the source of truth / database**; on top of it
there are two entry paths:

1. **Web app** (`web/`) — a FastAPI + HTMX app with a rich entry form and an editable
   **Mon–Fri weekly grid** (all weekdays always visible, blank = 0). See [Web app](#web-app).
2. **Notion Form** — the built-in Notion form, for casual/no-code entry.

A Python CLI (`src/`) creates the schema, seeds projects, and produces reports.

Each entry has a **project, date, hours, and description**. Who logged it is captured
automatically (see [Who submitted](#who-submitted) below) — no manual "person" field.

## How it works

- **Two Notion databases** live under a page called **Hours Tracker**:
  - **Projects** — `Name`, `Active`, `Client`
  - **Time Entries** — `Entry` (title), `Project` (relation → Projects), `Date`, `Hours`,
    `Description`, **`Logged by`** (auto-filled with the submitter), and two formula helpers
    `Week` / `Weekday` (for weekly views — see step 7)
- **Team members enter hours via a Form view** on Time Entries (Project, Date, Hours,
  Description). Every submission becomes a row.
- **Python scripts** (this repo) build the schema, bulk-seed projects, and report totals.

### The People roster

The web app's list of people (assignments columns, schedule rows, person dropdowns) comes
from a third database, **People** (`Name`, `Person` (people link), `Active`), created and
seeded from the current workspace members by:

```bash
./.venv/bin/python src/setup_people_db.py   # idempotent; re-run to pick up new members
```

Curate it in Notion: **untick `Active`** to hide someone, **retitle a row** to rename them
(e.g. turn a bare email into a proper name). Prefer unticking over deleting the row — the
seeder only skips people who still have a row, so a deleted person comes back next time it
runs. Rows must keep a `Person` link — rows without one are ignored. New workspace members
do NOT appear automatically; re-run the script (or add a row by hand) to include them. On
Render set `PEOPLE_DS_ID`; if it's unset the app falls back to listing all workspace
members, the old behavior.

## Who submitted

The form has **no Person field**. Instead, Time Entries has a `Logged by` property of type
*Created by*, which Notion fills automatically with whoever submits the form. This is more
reliable than asking people to pick themselves (and works even though this workspace's form
editor lacks the "autofill respondent" option).

> **Requirement:** the form's *"Who can respond"* must be **Only members of the workspace**
> (respondents signed in). With an anonymous public link, `Logged by` can't identify anyone.

`report.py` credits hours to `Logged by`. (If you ever re-add an explicit `Person` people
property, the report prefers that and falls back to `Logged by`.)

## One-time setup

### 1. Create a Notion integration (gets you a token)
1. Go to <https://www.notion.so/my-integrations> → **New integration**.
2. Name it (e.g. `hours-znlove`), pick your workspace, **Internal** type.
3. Under *Capabilities* keep **Read**, **Insert**, **Update**, and **Read user information**
   (the last one lets `Logged by` resolve to a name).
4. Copy the **Internal Integration Secret** (starts with `ntn_`).

### 2. Create the parent page and share it
1. In Notion, create a page called **Hours Tracker**.
2. On that page: **••• menu → Connections → Connect to → `hours-znlove`**.
3. Copy the page URL (or its 32-char id).

### 3. Fill in `.env`
```bash
cp .env.example .env
# then edit .env:
#   NOTION_TOKEN=ntn_...
#   NOTION_PARENT_PAGE=https://www.notion.so/Hours-Tracker-....
```

### 4. Install deps + build the databases
```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python src/setup_databases.py
```
Creates the **Projects** and **Time Entries** databases under your page and saves their
database + data source ids to `databases.json`.

### 5. Seed projects
```bash
./.venv/bin/python src/seed_projects.py "True Temper" "Vetilea" "NHMuseum"
```
Dedupe-safe: re-running with a longer list only adds what's new.

### 6. Add the Form view (in the Notion UI — the API can't create views)
On the **Time Entries** database: **`+` (add view) → Form**. Include these questions:

| Question | Setting |
|----------|---------|
| Project | required |
| Date | required, default **Today** |
| Hours | required |
| Description | optional |

Hide the **Entry** (title) question. Do **not** add a Person question — `Logged by` covers it.
Set **Who can respond → Only members of the workspace**, then **Share** and post the link.

### 7. (Optional) Reporting views in Notion
On Time Entries, add Table views grouped by **Logged by** or **Project**, with the Hours
column set to **Calculate → Sum**. Add a `Date is within → This week` filter for a live
weekly timesheet.

**Weekly timesheet (Mon–Fri, by person, editable).** Two helper formula properties support
this — `Week` (ISO week, e.g. `2026-W28`) and `Weekday` (e.g. `2 Tue`; the leading ISO
day-number makes groups sort Mon→Sun and lets you filter out weekends):
- Add a **Table** view → **Group → Logged by**.
- **Filter:** `Date is within → This week`, plus `Weekday does not contain Sat` and
  `Weekday does not contain Sun` (leaves Mon–Fri).
- **Sort:** `Weekday → Ascending`.
- Edit the **Hours** cell inline. Optionally **Sub-group → Weekday** for a per-day breakdown,
  or filter `Week is 2026-Www` to review a specific past week.

**See every day, even blank ones.** A grouped table only shows days that have entries. To see
all of Mon–Fri regardless, add a **Calendar** view instead: **Show as → Week**, **Show weekends
→ Off** (Mon–Fri), **Calendar by → Date**. Empty days appear as clickable empty cells; click one
to add an entry for that day. (Notion database views can't show placeholder rows for empty days;
the calendar's day grid is the native way to always see every weekday.)

## Web app

A FastAPI app that reads/writes the same Notion databases — a better entry experience than
Notion's form, plus the editable weekly grid Notion can't natively do.

```bash
./.venv/bin/uvicorn web.app:app --reload --port 8000
# then open http://localhost:8000
```

- **Login:** "Sign in with Notion" (OAuth), gated by an email allowlist — see [Auth](#auth).
- **`/reports` — Reports:** presets (this/last week/month) or custom range; totals, by-day
  chart, by-project bars, CSV export. Admins (`ADMIN_EMAILS` env var) get a **Team** scope
  with by-person totals and a team-wide CSV.
- **Timer:** start/stop on the Log hours page (Harvest-style); survives reloads, fills the
  Hours field rounded to the nearest 0.25h on stop.
- **Weekly extras:** capacity bar (`WEEK_TARGET_HOURS`, default 40) and a one-click
  "Copy last week's projects" on the grid.
- **Keep-alive:** a launchd job on the always-on mini pings `/healthz` every 10 min so the
  free Render instance never cold-starts (`com.jp.hours-keepalive`).
- **`/` — Log hours:** entries are stamped with the logged-in user, so just pick a project,
  date (defaults to today), hours, description → saves to Notion.
- **`/week` — Weekly grid:** rows are person × project, columns are **Mon–Fri** (every weekday
  always shown, even blank). Edit any cell to save instantly (upsert); clear a cell to remove
  that entry. Row/day/grand totals recompute live. Prev / This week / Next navigation. "Add a
  row" introduces a new person+project combination.
- **`/schedule` — Schedule (admins):** plan hours ahead in an Allocations database. Two zoom
  levels — **Weeks** (6 week columns; capacity heat map against `WEEK_TARGET_HOURS`) and
  **Days** (one week, Mon–Fri, heat against a fifth of that). Click a week header to plan
  that week day by day. Allocation rows carry real dates: day-cell edits save the exact day,
  a week-cell edit replaces the pair's whole week with one Monday-dated row (so week-planned
  hours appear in Monday's cell in the day view until spread out).

The chosen/logged-in person is written to a `Person` (people) property, **re-added on startup**
if missing. (Notion-form submissions still use `Logged by`; `report.py` and the grid read either.)

### Auth

Login uses **Notion OAuth**: the app reads the authorizing user's identity and checks their
email against `ALLOWED_EMAILS`. OAuth is only for *identity* — all data access uses the
integration token. Everyone not on the allowlist is rejected.

One-time setup:
1. On your integration at <https://www.notion.so/my-integrations> → **Distribution** →
   make it **public**. Copy the **OAuth client ID** and **secret**.
2. Add the redirect URI: `https://<your-host>/auth/callback`.
3. Set env vars: `NOTION_OAUTH_CLIENT_ID`, `NOTION_OAUTH_CLIENT_SECRET`,
   `NOTION_OAUTH_REDIRECT_URI`, `SESSION_SECRET`, and `ALLOWED_EMAILS` (comma-separated).

Local dev without OAuth: set `AUTH_DISABLED=1` to bypass login (never in production).

### Deploy (Render, free tier)

The repo includes `render.yaml`. In Render: **New + → Blueprint → connect this repo**, then in
the dashboard set the secret env vars (`NOTION_TOKEN`, `PROJECTS_DS_ID`, `TIME_ENTRIES_DS_ID`,
`ALLOCATIONS_DS_ID`, `PEOPLE_DS_ID`, the three `NOTION_OAUTH_*`, and `ALLOWED_EMAILS`;
`SESSION_SECRET` is auto-generated). Because `databases.json` is gitignored, the deploy reads
the data-source ids from those env vars (find the values in your local `databases.json`). Free instances sleep when idle
and wake on the next request (~30–60s) — fine for low traffic. After the first deploy you'll get
the host URL; set the Notion redirect URI to `https://<that-host>/auth/callback`.

**Notes:** each page makes a few Notion API calls (people + projects + entries), so expect a
short load on cold requests — fine for a team, cache later if needed.

## Daily use (CLI)

The form is the main entry path, but the CLI is handy for backfills and reports:

```bash
# log an entry (date defaults to today) — created via the API bot, so no submitter name
./.venv/bin/python src/log_hours.py --project "Mobile App" --hours 2.5 \
    --desc "Built the login screen"

# reports
./.venv/bin/python src/report.py                    # hours by person (submitter)
./.venv/bin/python src/report.py --by project
./.venv/bin/python src/report.py --since 2026-07-01
```

> Note: CLI entries are created by the API integration, so they have no human submitter
> in `Logged by`. Team entry goes through the form; use the CLI for backfills/corrections.

## Files
| File | Purpose |
|------|---------|
| `src/config.py` | Loads `.env`, builds the Notion client, parses page URLs → ids, persists db ids. |
| `src/setup_databases.py` | Creates the two databases (run once). |
| `src/setup_people_db.py` | Creates the People roster db + seeds it from workspace members (idempotent). |
| `src/seed_projects.py` | Bulk-adds projects, dedupe-safe. |
| `src/log_hours.py` | Logs one time entry from the CLI. |
| `src/report.py` | Aggregates hours by person (submitter) or project. |
| `web/app.py` | FastAPI routes: login/OAuth, form page, `/entry`, weekly grid, `/api/cell`. |
| `web/auth.py` | Notion OAuth flow + email allowlist. |
| `web/notion_ops.py` | Notion reads/writes for the web app (people, projects, entries, grid). |
| `render.yaml` | Render deploy blueprint (free tier). |
| `web/templates/` | Jinja2 templates (base, form, week). |
| `web/static/style.css` | App styling (light/dark aware). |
| `databases.json` | Auto-written database + data source ids (gitignored). |
| `.env` | Token + parent page (gitignored). |

## API note
Uses the Notion **2025-09-03 API** (notion-client ≥ 3): each database wraps a *data source*.
Schema lives on the data source, page parents use `data_source_id`, and queries go through
`notion.data_sources.query`. The scripts handle all of this; `databases.json` stores both
the database ids and the data source ids.
