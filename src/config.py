"""Shared config + Notion client for the hours tracker."""
import json
import os
import re
from pathlib import Path

import threading

from dotenv import load_dotenv
from notion_client import Client
from notion_client.errors import HTTPResponseError

ROOT = Path(__file__).resolve().parent.parent
DB_FILE = ROOT / "databases.json"

load_dotenv(ROOT / ".env")


class _RetryingClient(Client):
    """notion-client retries a 5xx only on GET/DELETE, but every database read
    here is a POST to `…/query` (and search is a POST too), so one Notion blip
    became an instant 500 page. Those POSTs are reads, so they're safe to retry
    like a GET. Writes (pages.create/update) still never retry — a 500 there may
    have landed, and retrying would duplicate the row."""

    _local = threading.local()   # the client is shared across request threads

    def _execute_with_retry(self, method, path, *args, **kwargs):
        self._local.read = method.upper() == "GET" or (
            method.upper() == "POST" and (path.endswith("/query") or path == "search"))
        return super()._execute_with_retry(method, path, *args, **kwargs)

    def _can_retry(self, error, method):
        if getattr(self._local, "read", False) and isinstance(error, HTTPResponseError):
            # Includes a bare 502/504 from Notion's edge, which carries no API code.
            return error.status in (429, 500, 502, 503, 504)
        return super()._can_retry(error, method)


def get_client() -> Client:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise SystemExit("NOTION_TOKEN is missing. Copy .env.example to .env and fill it in.")
    return _RetryingClient(auth=token)


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
    "allocations_db_id": "ALLOCATIONS_DB_ID",
    "allocations_ds_id": "ALLOCATIONS_DS_ID",
    "people_db_id": "PEOPLE_DB_ID",
    "people_ds_id": "PEOPLE_DS_ID",
    "invoices_db_id": "INVOICES_DB_ID",
    "invoices_ds_id": "INVOICES_DS_ID",
    "absences_db_id": "ABSENCES_DB_ID",
    "absences_ds_id": "ABSENCES_DS_ID",
    "goals_db_id": "GOALS_DB_ID",
    "goals_ds_id": "GOALS_DS_ID",
    "plan_db_id": "PLAN_DB_ID",
    "plan_ds_id": "PLAN_DS_ID",
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
