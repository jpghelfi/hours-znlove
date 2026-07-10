"""Shared config + Notion client for the hours tracker."""
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client

ROOT = Path(__file__).resolve().parent.parent
DB_FILE = ROOT / "databases.json"

load_dotenv(ROOT / ".env")


def get_client() -> Client:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise SystemExit("NOTION_TOKEN is missing. Copy .env.example to .env and fill it in.")
    return Client(auth=token)


def _extract_id(value: str) -> str:
    """Accept a raw 32-char id or a full Notion URL and return a dashed UUID."""
    if not value:
        raise SystemExit("NOTION_PARENT_PAGE is missing in .env")
    # Notion URLs end in a 32-char hex id (optionally after a title slug and dash).
    m = re.search(r"([0-9a-fA-F]{32})", value.replace("-", ""))
    if not m:
        raise SystemExit(f"Could not find a Notion id in: {value!r}")
    h = m.group(1)
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def get_parent_page_id() -> str:
    return _extract_id(os.environ.get("NOTION_PARENT_PAGE", ""))


def save_db_ids(ids: dict) -> None:
    DB_FILE.write_text(json.dumps(ids, indent=2))


_ENV_ID_KEYS = {
    "projects_db_id": "PROJECTS_DB_ID",
    "projects_ds_id": "PROJECTS_DS_ID",
    "time_entries_db_id": "TIME_ENTRIES_DB_ID",
    "time_entries_ds_id": "TIME_ENTRIES_DS_ID",
}


def load_db_ids() -> dict:
    """Database/data-source ids, from databases.json and/or env vars.

    Locally the file (written by setup_databases.py) is the source. On a deploy
    host where the file isn't present, set the *_DS_ID / *_DB_ID env vars instead;
    env values also override the file if both are set.
    """
    env_vals = {k: os.environ[v] for k, v in _ENV_ID_KEYS.items() if os.environ.get(v)}
    if DB_FILE.exists():
        data = json.loads(DB_FILE.read_text())
        data.update(env_vals)
        return data
    if {"projects_ds_id", "time_entries_ds_id"} <= env_vals.keys():
        return env_vals
    raise SystemExit(
        "No database ids: run src/setup_databases.py locally, or set PROJECTS_DS_ID "
        "and TIME_ENTRIES_DS_ID (plus *_DB_ID) env vars on the deploy host."
    )
