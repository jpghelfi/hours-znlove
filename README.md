# hours-znlove — Notion hours tracker

A time tracker backed by Notion. The team logs hours through a **Notion Form**; a Python
layer creates the schema, seeds projects, and produces reports.

Each entry has a **project, date, hours, and description**. Who logged it is captured
automatically (see [Who submitted](#who-submitted) below) — no manual "person" field.

## How it works

- **Two Notion databases** live under a page called **Hours Tracker**:
  - **Projects** — `Name`, `Active`, `Client`
  - **Time Entries** — `Entry` (title), `Project` (relation → Projects), `Date`, `Hours`,
    `Description`, and **`Logged by`** (auto-filled with the submitter)
- **Team members enter hours via a Form view** on Time Entries (Project, Date, Hours,
  Description). Every submission becomes a row.
- **Python scripts** (this repo) build the schema, bulk-seed projects, and report totals.

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

> Note: `log_hours.py --person` targets a `Person` property that the form-based schema
> doesn't include. Team entry goes through the form; use the CLI without `--person`.

## Files
| File | Purpose |
|------|---------|
| `src/config.py` | Loads `.env`, builds the Notion client, parses page URLs → ids, persists db ids. |
| `src/setup_databases.py` | Creates the two databases (run once). |
| `src/seed_projects.py` | Bulk-adds projects, dedupe-safe. |
| `src/log_hours.py` | Logs one time entry from the CLI. |
| `src/report.py` | Aggregates hours by person (submitter) or project. |
| `databases.json` | Auto-written database + data source ids (gitignored). |
| `.env` | Token + parent page (gitignored). |

## API note
Uses the Notion **2025-09-03 API** (notion-client ≥ 3): each database wraps a *data source*.
Schema lives on the data source, page parents use `data_source_id`, and queries go through
`notion.data_sources.query`. The scripts handle all of this; `databases.json` stores both
the database ids and the data source ids.
