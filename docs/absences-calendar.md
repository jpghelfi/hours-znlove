# Absences on the Google Calendar

Approved absences are mirrored onto znlove's shared **Absences** Google Calendar, so people see who's out in the calendar they already live in. Notion stays the source of truth; the calendar is a picture of it that this app keeps current. Code: `web/gcal.py`, called from `web/app.py`.

Off unless **`ABSENCES_CALENDAR_ID`** is set (and the `GOOGLE_*` credentials are). Unset, nothing changes anywhere.

## Lifecycle

| What happens in the app | On the calendar |
| --- | --- |
| Filed (Pending) | nothing |
| Approved (`POST /api/absence/decide`) | all-day event created — or updated, if it already exists |
| Declined, including correcting approved → declined | event removed |
| Re-approved | event recreated/updated |
| Deleted (`POST /api/absence/delete`) | event removed |

## The event

- Title `<Person> — Out`. The Absences db has no type column today; if one is ever added, it belongs in the title too.
- All-day: `start.date` = first day, `end.date` = last day **+ 1** (Calendar's all-day end is exclusive).
- **No reason, no decision note, no attendees** — the calendar is shared, the reason isn't.
- `transparency: transparent` (shows, but doesn't block anyone's free/busy).
- `extendedProperties.private.absenceId` = the Notion page id. That tag is how the reconcile tells our events from anything else on the calendar; untagged events are never touched.

## Idempotency

The event id is **derived from the Notion page id**: `absence` + the 32 hex digits of the page id. The Calendar API wants 5–1024 characters from base32hex's lowercase alphabet (`a–v`, `0–9`); hex is a subset of it, and so is the prefix — so the id is valid without any encoding, and one absence can only ever own one event.

- Insert answering **409** (the id exists — including an event deleted earlier, which Calendar keeps as `cancelled`) becomes a `PUT` of the same id with `status: confirmed`.
- Delete answering **404/410** is success.
- The id is also written to a rich-text **`Calendar event`** column on the absence row (added on boot by `ensure_absence_properties`), cleared when the event is removed. It isn't needed to find the event — the id is derivable — but it says in Notion *whether* the calendar has it.

## Failures

A courtesy, exactly like the absence emails: the decision is already saved in Notion when the calendar call runs, so any Google or Notion error is logged (`absence … not synced to the calendar`) and swallowed. Approving never fails because Calendar did. `google_auth.GoogleError` now carries the HTTP `status`, which is how 409/404/410 are told apart.

## Sync to calendar

A **Sync to calendar** button on `/absences` (admins and approvers, only while the feature is on) calls `POST /api/absence/calendar-sync`, which reconciles the window **60 days back → 365 ahead** (`gcal.SYNC_BACK_DAYS`/`SYNC_AHEAD_DAYS`):

1. every approved absence overlapping the window is upserted;
2. a pending/declined row that still records a `Calendar event` has it deleted;
3. every **tagged** event in the window whose absence isn't approved any more (or no longer exists — deleted straight in Notion) is deleted.

It answers `{synced, removed, failed}` and the page shows those counts. Use it once after setup (to put absences approved before this existed on the calendar) and whenever drift is suspected. It's one Notion read plus one Google call per absence, so it's slow-ish on a free instance but bounded.

## Setup (one-time)

1. **GCP project** (the one already used for Gmail/Sheets): APIs & Services → Library → enable **Google Calendar API**.
2. Re-run `./.venv/bin/python src/google_oauth_setup.py` locally and consent again — the scopes now include `calendar.events`. Sign in as the account that has **"Make changes to events"** access to the Absences calendar.
3. On Render, replace **`GOOGLE_REFRESH_TOKEN`** with the new one (client id/secret are unchanged).
4. On Render, set **`ABSENCES_CALENDAR_ID`** = `c_95537bbbb68c2064f1e9bb09c15d942bb6d97f8219c1d3a7c4b3751103d9cd7d@group.calendar.google.com`.
5. Open `/absences` and click **Sync to calendar** once.

Until step 3, Gmail and Sheets keep working on the old token; calendar calls just fail with a 403 that's logged and swallowed.

## Tests

`tests/test_gcal.py` — no network, no Notion: end-date exclusivity, event-id validity, approve/decline/delete hitting the right fake Google calls, 409 → update, 404/410 → success, failures swallowed, and the reconcile's counts and stray cleanup.
