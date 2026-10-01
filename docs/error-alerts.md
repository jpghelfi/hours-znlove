# Error alerts to Slack

When the app throws, a message lands in Slack instead of waiting for someone to
screenshot an "Internal Server Error" (`web/alerts.py`).

## What triggers one

- **Any uncaught error in a request** (`server_error` in `web/app.py`) — the user
  still gets a plain 500; Slack gets the method, path, who was logged in, the error
  and the tail of the traceback.
- **A Notion failure that survived the retries** (`notion_unavailable`, see
  `docs/notion-errors.md`) — titled *Notion unavailable* when it's an outage (the
  user saw the 503 "try again" page) and *Notion error* when it's a 400/404 bug.

Errors a route catches and turns into its own message (a 400 "could not file those
entries", a budget refusal) are handled behaviour, not failures, and don't alert.

## Not a flood

The same error on the same path is sent **once per 15 minutes**
(`ALERT_COOLDOWN_MIN` to change it). A Notion outage fails every page load, so
without this an hour of it would be hundreds of messages; with it, the next alert
after the cooldown says `+N more like this` so the count isn't lost. The cooldown is
per process and resets on a deploy, which is fine.

Sends run on a daemon thread with a 10 s timeout, so a slow Slack never delays the
error page, and a failed send is logged and dropped.

## Turning it on

1. In Slack: create an app → **Incoming Webhooks** → on → *Add New Webhook to
   Workspace* → pick the channel. Copy the `https://hooks.slack.com/services/…` URL.
2. In Render: set `SLACK_ALERT_WEBHOOK_URL` to it. Unset = alerts off; nothing else
   changes.

Tested by `tests/test_alerts.py` (no Slack, no Notion).
