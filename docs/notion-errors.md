# When Notion fails

Notion is the database, so a Notion hiccup is an outage of whatever page was loading.
On 2026-10-01 Notion's API spent about an hour answering intermittently with
`500 Cross-cell memcached access is not allowed` and timeouts — while
status.notion.so showed everything green — and the app showed a bare
"Internal Server Error" (Kepos on `/project`, among others).

## Reads retry, writes don't (`src/config.py`, `_RetryingClient`)

notion-client already retries 429s, and 5xx — but only for GET/DELETE, so that a
write is never sent twice. That rule missed this app entirely: every database read
is `POST data_sources/{id}/query` (and search is a POST too), so not one read was
ever retried on a 500.

`get_client()` now returns a subclass that treats those POSTs as the reads they
are: a query or search that gets a 429/500/502/503/504 is retried with the
library's own backoff (2 retries, ~1 s then ~2 s). The 502/504 case matters
because Notion's edge can answer with an HTML page and no API error code, which
the stock check doesn't recognise.

Writes — `pages.create`, `pages.update` — are **still never retried**. A 500 on a
write may have landed anyway; retrying would duplicate the entry. The Harvest sync
hit exactly that case and recovered by re-running, which is safe because it re-reads
before writing.

Which path a request is on is stashed in a `threading.local`, because one client is
shared across FastAPI's request threads.

## What the user sees when retries run out (`notion_unavailable` in `web/app.py`)

Any uncaught notion-client error becomes a **503** instead of a 500: a small page
saying Notion isn't answering, with a *Try again* button, or
`{"ok": false, "error": …}` for `/api/*` and non-GET requests so the page's
JavaScript shows a message instead of choking on HTML. It is logged with the path.

Tested by `tests/test_notion_retry.py` (a fake Notion over `httpx.MockTransport`,
no network).
