"""Tell Slack when the app errors (docs/error-alerts.md).

Posts to a Slack incoming webhook (`SLACK_ALERT_WEBHOOK_URL`); unset = off.
Sends happen on a daemon thread so an alert never slows the error page, a
failed send is logged and swallowed, and the same error (kind + path) is sent
at most once per `ALERT_COOLDOWN_MIN` (default 15) minutes — a Notion outage
fails every page load, and one message says that as well as fifty.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import urllib.request
from typing import Optional

_last_sent: dict[tuple, float] = {}
_suppressed: dict[tuple, int] = {}
_lock = threading.Lock()


def webhook() -> str:
    return os.environ.get("SLACK_ALERT_WEBHOOK_URL", "").strip()


def _cooldown() -> float:
    try:
        return float(os.environ.get("ALERT_COOLDOWN_MIN", "15")) * 60
    except ValueError:
        return 15 * 60


def _claim(key: tuple, now: float) -> Optional[int]:
    """None if this key is still cooling down; else how many repeats were
    swallowed since the last send (and the slot is taken)."""
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < _cooldown():
            _suppressed[key] = _suppressed.get(key, 0) + 1
            return None
        _last_sent[key] = now
        return _suppressed.pop(key, 0)


def message(kind: str, method: str, path: str, exc: BaseException,
            user: Optional[dict], repeats: int) -> str:
    who = (user or {}).get("email") or (user or {}).get("name") or "not logged in"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
    lines = [f":rotating_light: *hours-znlove — {kind}*",
             f"`{method} {path}` · {who}",
             f"*{type(exc).__name__}*: {str(exc)[:300]}"]
    if repeats:
        lines.append(f"_(+{repeats} more like this since the last alert)_")
    lines.append(f"```{tb}```")
    return "\n".join(lines)


def _post(url: str, text: str) -> None:
    try:
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        logging.exception("Slack error alert failed to send")


def notify(kind: str, method: str, path: str, exc: BaseException,
           user: Optional[dict] = None) -> bool:
    """Queue one alert. Returns whether it was sent (False when off or cooling)."""
    url = webhook()
    if not url:
        return False
    repeats = _claim((kind, type(exc).__name__, path), time.monotonic())
    if repeats is None:
        return False
    text = message(kind, method, path, exc, user, repeats)
    threading.Thread(target=_post, args=(url, text), daemon=True).start()
    return True
