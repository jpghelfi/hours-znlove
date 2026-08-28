# Say what you worked on

Every entry logged **from here on** has to carry either a description of at
least **10 characters** or a **linked Notion ticket**. Either arm satisfies it;
neither is optional on its own.

`docs/entry-validation-plan.md` is the analysis this was built from — what the
rule would have rejected across July and August, and the two things that had to
be solved before it could hold.

## What it binds, and what it deliberately doesn't

| Path | Enforced? | Why |
| --- | --- | --- |
| `POST /entry` — the log form, including the timer's stop | **yes** | where most entries are typed |
| `POST /api/cell` — the weekly grid, **when the cell creates an entry** | **yes** | where most *blank* entries came from |
| `POST /api/cell` — a cell that already holds hours | no | that write only moves the number; the entry keeps whatever it says |
| Blanking a cell to `0` (a delete) | no | there is nothing left to describe |
| `POST /api/entry/hours` — an admin correcting a number | no | it never touches the description, and blocking a correction because of an *old* entry's text would make bad data unfixable |
| `src/log_hours.py` (CLI) | no | an admin tool |
| `src/sync_harvest.py` | **never** | it writes `"Harvest"` as the whole description when a Harvest entry has no notes — 30 entries in the analysed window. Enforcing here would make the importer refuse hours somebody genuinely worked. `docs/budgets.md` settled this exact trade once already |
| Notion itself (its form, a hand-typed row) | can't be | Notion is the database; anyone with access can add a row |

That last line is the same honest framing the budget cap carries: **this binds
the app's UI, not the data.** Which is where every entry in the analysis came
from, so it is worth having anyway.

**Admins are not exempt.** The budget cap exempts them deliberately — they can
change the budget, so friction there only teaches them to route around it. This
is the opposite case: the rule exists so a client-facing export reads as work
rather than a column of numbers, and an admin's undescribed hour is exactly as
unreadable as anyone else's.

## Where the check lives

`notion_ops.require_note()`, called from `create_entry` and `set_cell` behind a
`note=` flag that **defaults to off** — the same shape as the budget cap's
`enforce=`, and for the same reason: every existing caller (the CLIs, the
importer, the tests) keeps working untouched, and only the two routes serving a
person typing into a browser turn it on. The routes pass the flag *in* rather
than having `notion_ops` reach for `auth`.

It runs **before** the budget check and before `_project_name_map()`, so a
refusal costs no Notion round trip — and "add a description" is a better first
answer than "this project is over budget" when both are true.

Whitespace doesn't count toward the length: `" " * 40` is a blank entry, and
`"hi     there"` is eight characters, not eleven.

## The weekly grid had nowhere to type

`/week` is a spreadsheet of hour cells; `POST /api/cell` carried
`{project_id, date, hours}` and nothing else. Every entry it created was born
blank — **72 of August's 89 rejections**, and 110 of the 129 rejected entries
carried whole-hour values, the shape a grid cell makes.

So the payload grew `description` / `task_url` / `task_label`, and the grid asks
for them **at the only moment it needs to**: when a cell that was empty is
filled in. Correcting `3` to `5` in a cell that already holds an entry is one
keystroke, exactly as before.

The prompt offers **the last few descriptions that person used on that project**
as one-tap chips (`_recent_notes`, three weeks back, deduped). Most cells are
yesterday's work continuing, and retyping the same sentence every morning is
precisely the friction that would make people resent the rule. The suggestion
list fails quietly to empty — a convenience is never a reason to fail a page
load.

That dialog renders from `base.html`'s `modals` block, outside every form, for
the reason `_task_dialog.html` records: a `required` control inside a closed
`<dialog>` inside a form makes the browser refuse a submit it can't focus
anything to explain.

## What a refusal looks like

- **The log form** checks in the browser first and refuses the submit with the
  app's own dialog — but never via an HTML `required` attribute, because the
  *ticket picker* can satisfy the rule instead and a `required` textarea would
  block a legitimately empty one. The Description label carries a live hint
  ("— 4 more characters, or link a ticket below") that turns green when either
  arm is met. The server refuses independently, with `?err=note`.
- **The grid** asks before the round trip. If the server refuses anyway
  (someone with the console open), the cell snaps back to its previous value
  rather than leaving a number on screen that was never written — the rule the
  budget cap already follows, and a **409** for the same reason: the request was
  well-formed, the rule refused it.

The message always names both ways out. "Say what you worked on — at least 10
characters — or link the Notion ticket."

## What this does and doesn't buy

A length floor stops **blank**, not **lazy**: `..........` passes. It is the
number that was asked for, and the ticket is the honest way past it for work
whose best description really is `ZN-999` — linking that ticket says more than
ten characters of prose would.

Past entries are untouched, by design. Nothing retroactively demands a
description of an entry logged before this shipped, and editing such an entry's
hours never asks for one.

## Tests

`./.venv/bin/python tests/test_notes.py` — 15 checks, no Notion calls, no pytest
dependency: both arms of the rule, whitespace not counting, the refusal naming
both ways out, the default staying off so the importer is untouched, the check
running before the Notion read, and the four grid cases — a new cell refused, a
new cell written with its description, a correction asking nothing, and a delete
asking nothing.

## Config

None. The rule is on, at 10 characters (`ops.MIN_DESCRIPTION`), which the log
form and the grid prompt both read from that one constant so they can't drift
from the server.
