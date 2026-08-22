# Pulling hours from Harvest with a button — implementation plan

*Written 2026-08-22. A plan, not a description: nothing below is built yet.
`docs/harvest-sync.md` describes the CLI that already exists and already ran.*

## The short version

About **80% of this already exists and has already run in production**.
`src/sync_harvest.py` did the July 2026 backfill on 2026-08-02: 3,043 Harvest
entries collapsed into 162 Notion rows, 463.91 h across 7 people. What is
missing is not a sync — it is the credentials wiring, a web module, a route, a
page, and an answer to what happens when a synced row is later edited or
deleted.

Two live checks against the Bear Group Harvest account shaped the rest of this
plan, and both are worth knowing before anything is built.

## 1. The token's role decides the design

A Harvest personal access token inherits the role of whoever mints it. Called
as JP today:

| Call | Result |
| --- | --- |
| `GET /v2/users` | **403 Forbidden** |
| `GET /v2/projects` | **1** project of ~15 (`Bear Website (Internal)`) |
| `GET /v2/time_entries` | works **account-wide** — 3,043 entries, 33 users |

JP's Harvest account is a `member` (`is_contractor: true`), and its permission
claims include `timers:read:all` but not `users:read`. So the earlier note that
"user/project listing 403s" is a **permissions** fact, not a quirk of the
connector — a token JP mints will behave exactly the same way.

What follows from it:

- **There are no emails**, ever, so email cannot be the mapping key.
- **The projects endpoint is useless** — 1 of 15.
- Everything must come from the objects embedded on each time entry:
  `user{id,name}`, `project{id,name}`, `client{id,name}`. That is enough.

> **Question 1, which blocks the mapping design: can a Bear Group Harvest
> *admin* mint the token instead?** An admin token unlocks `/v2/users` (emails)
> and `/v2/projects` (real ids) and makes §3 nearly free. If not, v1 proceeds on
> a member token with explicit mappings curated in Notion.

## 2. What must be set up

```
HARVEST_ACCOUNT_ID=
HARVEST_TOKEN=          # a PAT from https://id.getharvest.com/developers
```

The names are already fixed by `src/sync_harvest.py:68` — keep them. They go in
`.env` locally (loaded by `src/config.py:13`), blank-and-commented in
`.env.example`, as `sync: false` entries in `render.yaml`, and pasted into the
Render dashboard. `src/config.py` itself needs no change: its `_ENV_ID_KEYS` is
only for Notion database ids.

`harvest_enabled()` = both vars present, following `invoices_enabled()` and
`ticket_create_enabled()`, so the page says "not set up yet" instead of 500ing.
Deliberately *not* the `REPORT_EMAIL_ENABLED` pattern of a separate switch —
that exists only because one Google authorization serves two features.

## 3. Identity mapping — the real problem

### What the CLI does today

- **People:** normalized name tokens, "≥2 in common" (`src/sync_harvest.py:56-63,262`).
  Matches *Joaquin Heianna* → *Joaquin Kenta Heianna*.
- **Projects:** exact match, then prefix/containment, **longest Notion name
  wins**, plus a hardcoded `PROJECT_OVERRIDES` (`:48`).

### A latent bug on today's roster

The person matcher takes `hit[0]` with no ambiguity check. The roster holds
both **Juan Pablo Ghelfi** and **Pablo Saracca**; a Harvest user named
*Juan Pablo Saracca* shares two tokens with each and would be filed silently
under whichever sorts first. Someone's hours land on the wrong person with no
warning. The project matcher is equally silent.

### Recommendation: explicit mapping in Notion, fuzzy matching demoted to a suggestion

**The key is the Harvest user id** (an integer on every time entry). It is the
only field that cannot be renamed, and email is unavailable.

| Notion db | New property | Holds |
| --- | --- | --- |
| People | `Harvest User Id` (number) | the Harvest user id |
| Projects | `Harvest Project` (rich text) | comma-separated Harvest project ids |
| Time Entries | `Source` (select) | `Harvest` |
| Time Entries | `Harvest Sync` (rich text) | `{"ids":[…],"hours":7.5,"at":"…"}` |

All four added on startup by an `ensure_harvest_properties()`, exactly like
`ensure_admin_property()` and `ensure_task_properties()`.

`Harvest Project` is deliberately many-to-one: one Notion project legitimately
covers several budget-suffixed Harvest projects (`Vital Signals ×2` in the July
run). It also turns `PROJECT_OVERRIDES` from code into Notion data, which is
this codebase's standing rule — roster, access and project membership are all
curated in Notion.

**Direction of failure matters.** Many Harvest projects → one Notion project is
normal. One Harvest project → two Notion projects is an error and must be
**refused and named**, never resolved by "longest wins".

Unmatched rows go into three named buckets, always with their hours, never
silently dropped:

1. **Unknown people** — not on the roster. Expected and by design: 2,845 of
   July's 3,043 entries. Reads as "ignored, as designed".
2. **Unmapped projects** — the actionable bucket. Each gets a Notion-project
   dropdown and a "remember this", which writes `Harvest Project`.
3. **Ambiguous** — two roster hits, or two Notion projects. Refused until
   someone says which.

Name matching keeps working on day one, so nothing regresses; every confirmed
mapping makes the next run less guessy.

## 4. Writing into Time Entries

The CLI upserts on `(person, project, spent_date)`, discriminated by a
`Harvest` marker line at the top of the Description. Harvest-marked row →
update; **only hand-logged rows → skip and report**; nothing → create. That
third rule is exactly right and must survive everything below.

### (a) A live data-loss bug, today

`ops.set_cell` upserts on the same key and writes only `Hours` and `Person` —
it never touches the Description. `week_grid` sums every entry regardless of
source, so Harvest hours appear on `/week` as ordinary editable cells.

So: someone corrects a Harvest-imported Tuesday on the weekly grid →
`set_cell` edits **the Harvest row in place**, marker intact → the next sync
sees a Harvest row whose hours differ → **writes Harvest's number back over the
human's correction.**

**Fix:** `set_cell` clears `Source` when it edits a marked row. The row becomes
hand-logged and the next sync takes the *already-existing* "hand-logged → skip
and report" path. One line, no new state. The human wins permanently, which is
right: someone looked at it.

### (b) Why `Source` rather than the marker line

`Source` is **filterable in the Notion query**; a string prefix in a
description is not. That is what makes (c) affordable. Keep reading the old
marker as a fallback for one release and stamp `Source` on every row the sync
touches — it dies out on its own, no backfill.

### (c) Deleted in Harvest → orphaned forever

A Harvest entry deleted after a sync leaves its Notion row standing at the old
hours; nothing walks the other way. Same for a day whose Harvest total drops to
zero. **Fix:** a reconcile pass that loads every `Source = Harvest` row in the
range and archives the ones the plan doesn't cover — filtered on `Source`
(never a bare date range), capped and checked **before the first write** like
`clear_allocations`, always shown in the preview, and **off by default in v1**.

## 5. Scope and cost, measured

**Harvest:** July's 3,043 entries at `per_page=2000` → 2 requests. The rate
limit is 100 requests per 15 s. Unreachable.

**Notion, one month:** ~8 reads (5–10 s), then 162 creates at 0.3–0.6 s each →
**60–100 s** on a first import. A **re-run writes nothing** — every row reports
`unchanged`. Only the first pull of a period is expensive.

**Render free tier** has no documented request timeout, but 60–100 s in one
request is past what a browser and proxy stack should be trusted with — and
this app has *no* background-job infrastructure (no `BackgroundTasks`, no
queue, anywhere).

### Recommended v1: one week, one button, admins only, preview first

- Default period is the week on screen: ~40 rows, **15–25 s**, the same order
  as "Copy last week", which already ships and is accepted.
- **A month is done by chunking in the browser, not by a background job** — 4–5
  sequential POSTs of one week each, accumulating counters. No job state,
  resumable by clicking again (it is idempotent), every request stays short.
  This is the single most important call in the plan.
- Hard caps: refuse a range over 31 days, and refuse a plan of more than 500
  writes *before the first write*, so a mistyped range is refused whole rather
  than half-applied.

## 6. The screen

A new admin-only **`/sync`** page — not `/reports` (a read screen with its own
filter contract), not `/project` (already carrying invoices and three export
paths), not `/week` (per-person, non-admin). It is a two-step flow with its own
unmatched-row tables, and it is where the mapping curation lives.

- Period picker reuses `_period_range`, restricted to weekly and monthly the
  way `/absences` does.
- **Two buttons: Preview (writes nothing) and Apply.** Apply stays disabled
  until a preview has run for the current period — nobody's first click writes.
- Running: `setBusy` with `data-busy`; for a chunked month, a live
  "Week 2 of 5 — 47 rows written" line between requests.
- Afterwards: created / updated / unchanged / skipped (hand-logged) /
  zero-hour dropped / would-delete, the three unmatched buckets with hours, and
  the per-person and per-project breakdown the CLI already prints.
- Options as checkboxes, defaulting to the July run's flags — including
  non-billable projects, which matters because Harvest marks *whole projects*
  non-billable and internal work is invisible without it.

## 7. Failure modes

| Risk | Handling |
| --- | --- |
| Partial failure mid-write | No transaction is possible. Idempotent, so clicking again finishes it; deterministic write order so a resumed run redoes the prefix as `unchanged`; per-row `try` so one bad row can't abort a week; return the counters collected *before* the exception, so the user sees "84 written, failed on 2026-07-18 / Pablo / Neurogum" rather than a bare 500. |
| Harvest 429 | Unreachable at 2–3 requests a month. Still honor `Retry-After` once, then say so in plain English. |
| **Notion rate limit** | The real risk. Take `_write_lock` **per row, not per run** — the opposite of `set_allocation_range`. A 100-write sync holding the lock would freeze every `/week` save in the app for a minute; each row is independently idempotent, so fairness beats atomicity. |
| One Harvest project → two Notion projects | Refuse at preview and name both. Do not keep "longest wins". |
| Two roster people → one Harvest user | Same: refuse and name, never `hit[0]`. |
| Timezones | A non-issue, verified: Harvest's `spent_date` is a plain date string and we store plain ISO. Keep the Python-side range re-check as paging insurance. Do **not** use `updated_since` for incremental syncs without first solving deletes — a deleted entry never appears in an updated-since window. |
| Running timers | A running timer has partial hours; filter `is_running` out. The CLI doesn't, so a mid-day sync imports a half-finished timer. |
| Dry run | Exists in the CLI, but is **dishonest**: it skips the "what already exists" read, so it cannot say "3 already exist unchanged" or "2 collide with hand-logged rows". The web preview must do that read and classify every row. This is the biggest single upgrade over the CLI. |

## 8. Files

Imports follow the existing rule — web imports from src, never the reverse.

| File | Role |
| --- | --- |
| `src/harvest_api.py` *(new)* | Transport only, no Notion. `enabled()`, `time_entries(from, to)` with the `next_page` loop, `explain(exc)`. Use **httpx** — already a dependency and already used in `auth.py`. |
| `src/harvest_sync.py` *(new)* | The **pure** logic, no I/O: `norm`/`name_tokens`, `match_person`, `match_project`, `plan(entries, roster, projects, existing, opts)`. Callers supply the data, so both front doors share one matcher and one set of upsert rules. |
| `web/harvest_ops.py` *(new)* | The web adapter: `ensure_harvest_properties()`, `preview()` (reads only), `apply()` (per-row lock, per-row try), `set_person_harvest_id()`, `set_project_harvest_ids()`. Reuses `list_people` / `list_projects` / `entries_between` instead of the CLI's duplicates. |
| `web/app.py` | `GET /sync`, `POST /api/harvest/preview`, `POST /api/harvest/apply`, `POST /api/harvest/map` — each with the standard login → admin → same-origin preamble. **Apply re-plans server-side and never trusts a client-sent plan**, the `set_entry_hours` lesson. Plus `ensure_harvest_properties()` in `_startup()`. |
| `web/templates/sync.html` *(new)* | The page, and a nav entry in `base.html`. |
| `src/sync_harvest.py` | Stays as the CLI, reduced to argparse plus calls into the two new src modules. One matcher, one set of rules, two front doors. |
| `.env.example`, `render.yaml` | The two credentials. |
| `docs/harvest-sync.md` | Updated in place with the web section, the new properties, the deletion story and the permissions finding — not duplicated. |

## Open questions, in the order they block things

1. **Who mints the Harvest token?** Blocks the mapping design. JP's is a
   `member`: verified 403 on users, 1 of 15 projects. Enough for v1, but an
   admin token would make §3 nearly free. Worth one message before building the
   mapping UI.
2. **Mapping curated in Notion, or in code?** Recommend Notion — four new
   columns, consistent with every other curation decision here.
3. **Who wins when someone edits a synced row on `/week`?** Recommend the
   human. This is a live bug today, independent of the button.
4. **Should a Harvest deletion delete the Notion row?** Recommend yes, off by
   default, always previewed, capped.
5. **Is a month button wanted in v1,** or is a week enough to start?
6. **Should "import non-billable" be a checkbox on the Notion project row**
   rather than a per-run option? July needed it explicitly and always will.
7. **Can non-admins pull their own week?** Recommend no in v1.
