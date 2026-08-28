# Requiring a description or a ticket on every logged hour — analysis

**Status: analysis only. Nothing built.** The rule as asked: from now on, every
logged entry must carry either a **description of more than 10 characters** or a
**linked Notion ticket**.

The rule is sound and worth having. Two things in the way are worth deciding
before any of it is written: **the weekly grid has nowhere to put either**, and
**a 10-character floor rejects a lot of genuinely useful descriptions**.

## What the rule would have done to the last two months

Scored against every entry the app can see (`/project.csv`, May–August 2026):

| Month | Entries | Would pass | **Would be rejected** | of which blank | of which too short |
| --- | ---: | ---: | ---: | ---: | ---: |
| July | 208 | 168 | **40 (19 %)** | 9 | 31 |
| August | 290 | 201 | **89 (31 %)** | 72 | 17 |
| **Total** | **498** | 369 | **129 (26 %)** | 81 | 48 |

**353 of 1 192 hours (30 %)** sit behind an entry the rule would have refused.

It does not land evenly:

| Person | Rejected / logged | |
| --- | ---: | ---: |
| Valery Nontol | 29 / 32 | **91 %** |
| Joel Avero | 16 / 21 | **76 %** |
| Juan Pablo Ghelfi | 21 / 53 | 40 % |
| Melisa Bellico | 17 / 45 | 38 % |
| Lautaro Ayub | 13 / 50 | 26 % |
| Joaquin Kenta Heianna | 8 / 36 | 22 % |
| Zarco Nontol | 7 / 41 | 17 % |
| Pablo Saracca | 14 / 88 | 16 % |
| Francisco Andres | 1 / 46 | 2 % |
| Vanessa Paolini · Matias Olivera · Cristina Lin | 0 | 0 % |

Two people would meet this rule on nearly every entry they log, and three
already meet it on all of them. That is not an argument against the rule — it is
the argument *for* it — but it is a change to two people's daily habit, not a
setting, and it lands the day it ships.

**One encouraging number:** 41 August entries pass **only** because they carry a
ticket. The escape hatch is already doing real work, unprompted.

## Two things the rule breaks as written

### 1. The weekly grid can't satisfy it at all

`/week` is a Mon–Fri grid of hour cells. `POST /api/cell` takes
`{project_id, date, hours}` — **there is no description field in the payload,
none in the UI, and no ticket picker**. Every entry it creates is born with an
empty description.

That is most of the damage: **72 of August's 89 rejections are blank
descriptions**, and 110 of the 129 rejected entries carry whole-hour values —
the shape a grid cell produces.

So the rule can't simply be switched on. Either the grid gets somewhere to type,
or it gets exempted — and exempting it makes the rule optional in practice,
because the grid is the faster way to log and everyone already knows it.

Options, cheapest first:

| | What | Cost | Honest verdict |
| --- | --- | --- | --- |
| **A** | Exempt `/api/cell` | none | The rule becomes advisory. Whoever finds this out first stops using the form. |
| **B** | A cell that's being *filled for the first time* opens a small popover: hours + description/ticket. Editing an existing cell's hours stays one keystroke. | ~1 day | **Recommended.** Costs nothing on the common action (correcting a number) and asks once, when the work is actually being described. |
| **C** | A description column on the grid | ~half a day | Fights the grid's whole point — it is a spreadsheet of numbers, and a text column per person-project-week doesn't fit the layout. |
| **D** | Drop the grid | — | It is the most-used screen. No. |

### 2. Ten characters rejects real descriptions and passes fake ones

The descriptions the rule would refuse today are mostly *not* lazy:

```
30x  "Harvest"      (7)   ← every entry the Harvest importer writes with no notes
 2x  "PR Review"    (9)
 2x  "ZN-999"       (6)   ← a ticket reference, which is exactly the thing wanted
 1x  "ZN-1133"      (7)
 1x  "TB-54"        (5)
 1x  "Text PDP"     (8)
 1x  "Rebuy"        (5)
 1x  "banners"      (7)
```

`ZN-999` and `TB-54` are *more* useful than most ten-character sentences — they
name the ticket. And the descriptions that only just clear the bar are no better:
`Harvest\nBW-30` (13) passes purely because the importer's marker pads it.

Meanwhile `..........` passes, and so does `asdfghjkla`. A length floor stops
**blank**, not **lazy** — worth being clear-eyed that this buys "something was
typed", not "the work was described".

Suggested adjustment: **keep the floor low (5–6 characters, or simply
non-blank), and put the effort into making the ticket the easy path instead.**
A ticket is machine-checkable, links to real context, and already carries 41
entries a month unprompted. If a 10-character rule is what you want anyway, the
`ZN-999`-shaped descriptions need an exemption — a short-code pattern
(`^[A-Z]{2,4}-\d+$`) that counts as a reference rather than a description.

## The Harvest importer would fail on every no-note entry

`src/sync_harvest.py` writes `"Harvest"` as the description when a Harvest entry
has no notes — **30 entries in this window**. If validation lives in
`create_entry`, the importer starts refusing to import hours somebody genuinely
worked. That must not happen: `docs/budgets.md` already settled this exact
question for the budget cap — "refusing to import hours somebody genuinely
worked would corrupt the record to protect a number" — and the answer was an
`enforce` flag defaulting to off, so the CLIs are untouched.

**Follow that precedent exactly**: `require_note=False` by default on
`create_entry`, turned on only by the two routes that serve a person typing into
a browser.

## Where the check goes

Every write path, and what should happen to each:

| Path | Today | Under the rule |
| --- | --- | --- |
| `POST /entry` (log form, incl. the timer's stop) | description optional, ticket optional | **enforced** |
| `POST /api/cell` (weekly grid) | no description exists | **enforced, once option B gives it somewhere to type** |
| `POST /api/entry/hours` (admin fixes a number) | hours only | **not enforced** — it never touches the description, and blocking a correction because of an old entry's text would make bad data unfixable |
| `src/log_hours.py` (CLI) | `--desc` optional | not enforced (admin tool) — or `--desc` becomes required, which is a one-line change and its own decision |
| `src/sync_harvest.py` | writes `"Harvest"` | **never enforced** (see above) |
| Notion directly (form, manual row) | — | **cannot be enforced.** Notion is the database; anyone with access can type a row |

That last line is the same honest framing the budget cap carries: this binds
*this app's UI*, not the data. Which is fine — it is where 100 % of the entries
in this analysis came from.

The check belongs in `notion_ops.create_entry`, beside the budget check, for the
same reason: it is the one place that sees the (description, ticket) pair on
every path, and the routes pass `enforce` *in* rather than having `notion_ops`
reach for `auth`.

## Are admins exempt?

The budget cap exempts them, deliberately — they can change the budget, so
friction there only teaches them to route around it. **This is the opposite
case.** The rule exists so that a client-facing export reads as work rather than
numbers, and an admin's undescribed hour is exactly as unreadable as anyone
else's. Recommend **no exemption** — and note that admins are two of the four
heaviest hitters in the table above (Juan Pablo 40 %, Zarco 17 %).

## What it should feel like

- The form's Description field gets `required` semantics **only in the sense
  that the save is refused** — never an HTML `required` attribute on a field
  that can be satisfied by the *ticket picker instead*. (And never a `required`
  control inside a closed dialog: that is the bug that silently broke saving
  the day the ticket dialog shipped — see `_task_dialog.html`.)
- The refusal has to name the alternative, not just the failure: *"Add a few
  words about what you did, or link the ticket."*
- The grid's popover should default focus to the description, and offer the
  last few descriptions that person used on that project — most cells are the
  continuation of yesterday's work, and one tap beats retyping.
- On `/api/cell` the refusal is a **409** and the cell snaps back to its old
  value, exactly as the budget cap already does, because leaving the typed
  number on screen reads as saved.

## Backfill: what about the 129 that already exist?

"From now on" means existing entries stay. But they will keep showing up in
client exports as blank lines. Worth pairing the rule with a way to fix them:
`/project`'s entry list already renders every entry with an editable Hours cell
— making the **description** editable there too is a small change and turns the
backlog into something an admin can clear in one sitting, the way the goals
sweep works.

## Recommendation

1. **Ship the check in `create_entry`** behind an `enforce`-style flag, on
   `POST /entry` first — a one-day change that covers the log form and the
   timer, and immediately stops new blank entries from the path most people use.
2. **Then the grid popover (option B)**, which is where the volume actually is.
   Until it exists, the grid is exempt and the rule is partial — say that out
   loud rather than letting it look complete.
3. **Set the floor at non-blank or ~5 characters, not 10**, unless short ticket
   codes get an exemption; the ten-character version rejects `ZN-999` while
   passing `..........`.
4. **Leave the CLIs and the Harvest importer alone**, per the budgets precedent.
5. **Make descriptions editable on `/project`** so the 129 existing entries can
   be cleaned up rather than living forever in the exports.
