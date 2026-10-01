#!/usr/bin/env python3
"""Tests for Slack error alerts (web/alerts.py).

Run:  ./.venv/bin/python tests/test_alerts.py

Plain asserts and a tiny runner, like the other tests. Nothing here reaches
Slack or Notion: the send is swapped for a list that records messages.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SESSION_SECRET", "test-secret-not-used-for-anything")

from notion_client.errors import APIResponseError  # noqa: E402
from web import alerts  # noqa: E402

_FAILS: list[str] = []
SENT: list[str] = []


def check(name):
    def deco(fn):
        alerts._last_sent.clear()
        alerts._suppressed.clear()
        SENT.clear()
        os.environ["SLACK_ALERT_WEBHOOK_URL"] = "https://hooks.slack.test/x"
        try:
            fn()
            print(f"  ok   {name}")
        except Exception:
            print(f"  FAIL {name}")
            _FAILS.append(name + "\n" + traceback.format_exc())
        return fn
    return deco


class _SyncThread:
    """Run the send inline so tests can look at it straight away."""
    def __init__(self, target, args, daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


alerts._post = lambda url, text: SENT.append(text)
alerts.threading = type("T", (), {"Thread": _SyncThread, "Lock": threading.Lock})


def boom():
    try:
        raise ValueError("bad thing")
    except ValueError as e:
        return e


@check("off when no webhook is configured")
def _():
    os.environ["SLACK_ALERT_WEBHOOK_URL"] = ""
    assert alerts.notify("500 error", "GET", "/x", boom()) is False
    assert SENT == []


@check("one alert names the path, the user and the error")
def _():
    assert alerts.notify("500 error", "GET", "/project", boom(),
                         {"email": "jp@znlove.xyz"})
    assert len(SENT) == 1
    assert "GET /project" in SENT[0] and "jp@znlove.xyz" in SENT[0]
    assert "ValueError" in SENT[0] and "bad thing" in SENT[0]


@check("the same error on the same path is sent once per cooldown")
def _():
    for _ in range(5):
        alerts.notify("500 error", "GET", "/project", boom())
    assert len(SENT) == 1


@check("a different path is a different alert")
def _():
    alerts.notify("500 error", "GET", "/project", boom())
    alerts.notify("500 error", "GET", "/week", boom())
    assert len(SENT) == 2


@check("after the cooldown, the next alert counts what was swallowed")
def _():
    os.environ["ALERT_COOLDOWN_MIN"] = "0"
    try:
        alerts.notify("500 error", "GET", "/p", boom())
        os.environ["ALERT_COOLDOWN_MIN"] = "15"
        alerts.notify("500 error", "GET", "/p", boom())
        alerts.notify("500 error", "GET", "/p", boom())
        alerts._last_sent.clear()          # cooldown over
        alerts.notify("500 error", "GET", "/p", boom())
    finally:
        os.environ.pop("ALERT_COOLDOWN_MIN", None)
    assert len(SENT) == 2 and "+2 more" in SENT[1], SENT


def _app():
    from fastapi.testclient import TestClient
    from web.app import app

    @app.get("/__crash")
    def crash():
        raise RuntimeError("kaboom")

    @app.get("/__notion_down")
    def notion_down():
        raise APIResponseError(code="internal_server_error", status=500, message="Cross-cell",
                               headers=httpx.Headers(), raw_body_text="")

    return TestClient(app, raise_server_exceptions=False)


@check("an uncaught error in a route is a 500 and an alert")
def _():
    r = _app().get("/__crash")
    assert r.status_code == 500
    assert len(SENT) == 1 and "kaboom" in SENT[0] and "500 error" in SENT[0]


@check("a Notion outage alerts too, and still shows the 503 page")
def _():
    r = _app().get("/__notion_down", headers={"accept": "text/html"})
    assert r.status_code == 503
    assert len(SENT) == 1 and "Notion unavailable" in SENT[0]


if __name__ == "__main__":
    print(f"\n{len(_FAILS)} failed\n" if _FAILS else "\nall passed\n")
    for f in _FAILS:
        print(f)
    sys.exit(1 if _FAILS else 0)
