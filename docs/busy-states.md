# Busy states — telling people the app is working

Notion is the database, so nothing here is instant: every page re-queries
Notion server-side, every save writes back to it, and on Render's free tier a
cold instance adds seconds on top. Before this, a click produced no visible
change at all — people assumed it hadn't registered and clicked again. On the
write paths that meant duplicate work, not just a doubled wait.

Three pieces, all wired from `web/templates/base.html`.

## 1. `navprog` — the top progress bar

`<div class="navprog">` sits at the top of `<body>`, `position: fixed`,
`z-index: 20` (above the sticky topbar), hidden by default. A delegated
listener shows it on:

- any left-click on a same-origin `<a href>`, and
- any `submit` event.

It hides on `pageshow` (which covers bfcache restores — otherwise Back hands
you a page with the bar still running) and `pagehide`, plus a 30s safety
timeout for navigations the browser quietly drops.

Deliberately skipped:

- modified clicks (⌘/ctrl/shift/alt) and middle-clicks — those open new tabs
- `target="_blank"` and `[download]`
- hash-only links (no load happens)
- **`*.csv`** — a download never navigates, so no `pageshow` would ever fire to
  stop the bar. CSV export therefore still has no feedback; it needs its own
  treatment (fetch → blob, or a timed busy state on the link).

### `form.submit()` fires no submit event

Several selects auto-submit their form on change (`/schedule` person+project
filters, `/project` period picker + project select). Calling `form.submit()`
programmatically **bypasses the `submit` event**, so the delegated listener
never sees it. Those four call sites say `onchange="navStart(); this.form.submit()"`
instead. `window.navStart()` is exported for exactly this.

This one mattered most: the dropdown visually commits the instant you pick,
so the page *looks* applied while the old grid is still on screen.

## 2. `setBusy(btn, on)` — spinner in a button

Adds `.is-busy` (CSS spinner via `::before`, `cursor: progress`), sets
`disabled`, and — if the button carries `data-busy="…"` — swaps its label,
stashing the original in `data-idle-label` so the restore is exact. Exported as
`window.setBusy`.

## 3. `form[data-lock]` — one submit, once

A form marked `data-lock` sets `data-sent` on first submit and `preventDefault`s
every submit after that; its submit button goes through `setBusy`. Both are
cleared on `pageshow` so a bfcache Back doesn't leave a dead button.

Used by the log-hours form (`form.html`), whose button carries
`data-busy="Saving to Notion…"`. Before this, double-clicking Save filed the
same entry twice — a data bug, not just a perception one.

The lock runs in a listener registered **before** the `navStart` one, so a
blocked re-submit marks the event handled and doesn't also restart the bar.
Disabling on `submit` (not on `click`) is what keeps the submission itself
intact.

## The schedule popover

`/schedule`'s assign dialog is the slowest action in the app:
`set_allocation_range` (`web/notion_ops.py`) upserts **one Notion row per
weekday in the range, sequentially, under `_write_lock`**. "Repeat through" a
month is ~20 round-trips.

`savePop` now:

- refuses to re-enter while `POP_BUSY`
- disables the select, hours, repeat-through, both action buttons and the ×
- labels the pressed button with the real work — `Saving 23 days…` /
  `Removing 23 days…` — counted client-side by `weekdaysBetween()`, which
  mirrors the server's weekday skipping so the number is honest
- blocks Esc (`cancel` event) while in flight, since closing drops the row
  reference the response needs to repaint
- captures `POP.row` / `POP.date` before the `await`, and restores everything
  in a `finally`

`dialog.pop.is-busy label { opacity: .6 }` greys the fields so a frozen dialog
reads as "working", not "broken".

## Still without feedback

- **CSV exports** (`/reports.csv`, `/project.csv`) — see the skip above.
- **Weekly-grid cells** and **assignment checkboxes** keep their existing
  `.saving` tint, but stay editable/clickable mid-flight, so fast repeated
  edits can still race.
