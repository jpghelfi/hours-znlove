# hours-znlove — Notion hours tracker

Manual time entry into Notion via the Notion API. Each entry has a **person, project,
date, hours, and description**. This repo creates the Notion schema for you and gives you
CLIs to log hours and report on them.

> Prefer zero code? See `NOTION_SETUP.md` for the pure-Notion (database + Form) version.
> This repo is for when you want automation, imports, or custom reports.

## One-time setup

### 1. Create a Notion integration (gets you a token)
1. Go to <https://www.notion.so/my-integrations> → **New integration**.
2. Name it (e.g. `hours-znlove`), pick your workspace, **Internal** type.
3. Under *Capabilities* keep **Read**, **Insert**, and **Update** content.
4. Copy the **Internal Integration Secret** (starts with `ntn_`).

### 2. Create a parent page and share it
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

### 4. Build the databases
```bash
./.venv/bin/python src/setup_databases.py
```
Creates the **Projects** and **Time Entries** databases under your page and saves their
ids to `databases.json`.

## Daily use

```bash
# add some projects
./.venv/bin/python src/seed_projects.py "Website Redesign" "Mobile App"

# log hours (date defaults to today; --person matches a workspace member by name)
./.venv/bin/python src/log_hours.py --person "Jane" --project "Mobile App" \
    --hours 2.5 --desc "Built the login screen"

# reports
./.venv/bin/python src/report.py                    # by person
./.venv/bin/python src/report.py --by project
./.venv/bin/python src/report.py --since 2026-07-01
```

## Add a Form for non-CLI entry
The scripts create real Notion databases, so you can still open **Time Entries** in Notion
and add a **Form view** (Person, Project, Date, Hours, Description) for teammates who'd
rather click than type commands. Both paths write to the same place.

## Files
| File | Purpose |
|------|---------|
| `src/config.py` | Loads `.env`, builds the Notion client, resolves ids. |
| `src/setup_databases.py` | Creates the two databases (run once). |
| `src/seed_projects.py` | Adds projects. |
| `src/log_hours.py` | Logs one time entry. |
| `src/report.py` | Aggregates hours by person or project. |
| `databases.json` | Auto-written database + data source ids (gitignored). |

## API note
Uses the Notion **2025-09-03 API** (notion-client ≥ 3): each database wraps a *data source*.
Schema lives on the data source, page parents use `data_source_id`, and queries go through
`notion.data_sources.query`. The scripts handle all of this; `databases.json` stores both
the database ids and the data source ids.
