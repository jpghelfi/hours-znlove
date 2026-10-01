#!/usr/bin/env python3
"""Tests for Notion blips: read retries, write non-retries, the 503 page.

Run:  ./.venv/bin/python tests/test_notion_retry.py

Plain asserts and a tiny runner, like the other tests. **Nothing here touches
Notion** — a fake httpx transport plays Notion, failing on cue.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-for-anything")

from config import _RetryingClient  # noqa: E402
from notion_client.errors import APIResponseError  # noqa: E402

_FAILS: list[str] = []


def check(name):
    def deco(fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception:
            print(f"  FAIL {name}")
            _FAILS.append(name + "\n" + traceback.format_exc())
        return fn
    return deco


def fake_notion(failures: int, status: int = 500):
    """A client whose Notion fails `failures` times, then answers. Returns
    (client, calls) — calls counts every request that reached the fake."""
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) <= failures:
            if status == 502:   # Notion's edge: HTML, no API error code
                return httpx.Response(502, text="<html>Bad Gateway</html>")
            return httpx.Response(status, json={
                "object": "error", "status": status, "code": "internal_server_error",
                "message": "Cross-cell memcached access is not allowed"})
        return httpx.Response(200, json={"object": "list", "results": [], "has_more": False})

    client = _RetryingClient(
        auth="secret_test", client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry={"max_retries": 2, "initial_retry_delay_ms": 1, "max_retry_delay_ms": 1})
    return client, calls


@check("a data source query (a POST) survives two Notion 500s")
def _():
    client, calls = fake_notion(failures=2)
    assert client.data_sources.query(data_source_id="ds")["results"] == []
    assert calls == ["POST"] * 3


@check("a 502 from Notion's edge (no JSON body) is retried too")
def _():
    client, calls = fake_notion(failures=1, status=502)
    client.data_sources.query(data_source_id="ds")
    assert len(calls) == 2


@check("search is a read and is retried")
def _():
    client, calls = fake_notion(failures=1)
    client.search(query="x")
    assert len(calls) == 2


@check("a read still gives up after max_retries")
def _():
    client, calls = fake_notion(failures=5)
    try:
        client.data_sources.query(data_source_id="ds")
    except APIResponseError:
        pass
    else:
        raise AssertionError("expected the 500 to surface")
    assert len(calls) == 3


@check("creating a page is never retried (the 500 may have landed)")
def _():
    client, calls = fake_notion(failures=1)
    try:
        client.pages.create(parent={"data_source_id": "ds"}, properties={})
    except APIResponseError:
        pass
    else:
        raise AssertionError("a write must not be retried")
    assert calls == ["POST"]


@check("updating a page is never retried")
def _():
    client, calls = fake_notion(failures=1)
    try:
        client.pages.update(page_id="p", properties={})
    except APIResponseError:
        pass
    assert calls == ["PATCH"]


@check("a Notion failure renders a 503 page, and JSON on /api")
def _():
    from fastapi.testclient import TestClient
    from web.app import app

    @app.get("/__boom")
    def boom():
        raise APIResponseError(code="internal_server_error", status=500,
                               message="Cross-cell", headers=httpx.Headers(), raw_body_text="")

    @app.get("/api/__boom")
    def api_boom():
        return boom()

    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/__boom")
    assert r.status_code == 503 and "Try again" in r.text, r.status_code
    r = tc.get("/api/__boom")
    assert r.status_code == 503 and r.json()["ok"] is False


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
