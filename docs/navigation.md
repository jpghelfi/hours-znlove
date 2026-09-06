# Navigation

How the app's menu is built and why it looks the way it does. The review that
led here is `docs/ux-review-2026-09-06.md`.

## The map

Pages are grouped by the job they do, not by who may open them:

| Group   | Pages                                   | Who        |
|---------|-----------------------------------------|------------|
| Track   | Log hours `/`, My week `/week`, Absences `/absences` | everyone |
| Plan    | Schedule `/schedule`                    | everyone reads, admins plan |
| Reports | Reports `/reports`, By project `/project`, Budgets `/budgets` | admins |
| Admin   | Invoices `/invoices`, Assignments `/assignments` | admins |

## Desktop (> 720px)

One row: brand · the four Track/Plan pages as flat links · **Reports ▾** ·
**Admin ▾** · the account avatar. The two groups are native `<details>`
dropdowns (`.navmenu`), the same mechanism as the people/project filter
pickers, so they work without JavaScript; a small script in `base.html` closes
a menu on an outside click or Esc and keeps only one open at a time. A group's
trigger is highlighted when any page inside it is current, and the page itself
is highlighted inside the list. Each item carries a one-line description so
"Reports" vs "By project" is explained where the choice is made.

The account avatar (initials) opens the user menu: name and email, the
dark-mode toggle, Log out. Those used to sit loose in the bar; log out is a
once-a-month action and the theme toggle a once-ever one.

## Phone (≤ 720px)

The top bar keeps only the brand and the avatar. Navigation moves to a
**tab bar pinned to the bottom** (`.tabbar`): Log · Week · Schedule ·
Reports (admins) / Absences (everyone else) · **More**. More opens a bottom
sheet (`dialog.sheet`, built on the app's `zdlg` dialog) with the whole map,
grouped as above, current page highlighted. `main` gets bottom padding for the
bar and the planner's own scroller subtracts `--tabbar-h`, so nothing hides
under it; the bar respects `env(safe-area-inset-bottom)` on iPhones.

Before this the nine links wrapped to three ragged rows inside a sticky
header, which took a third of a phone screen on every page.

## How "which page is current" works

Each page template overrides one `nav_*` block with the word `active`
(`{% block nav_week %}active{% endblock %}`), as it always did. `base.html`
now defines those blocks inside a swallowed `{% set %}` so the word never
prints, then renders them once via `self.nav_*()` into a `nav` dict. The
desktop links, the group triggers, the tab bar and the More sheet all read
that one dict. A new page needs: a `nav_<name>` block in the swallowed set, an
entry in `nav`, and a link in the desktop group, the sheet, and (if it earns
one) the tab bar.

## Period navigation

Every page that moves through time uses the same trio: `←` · **[This week /
This month]** · `→` (`.weeknav.arrows`), with `aria-label`s on the arrows. It
replaced five vocabularies (Prev/Next, Earlier/Later, Now, bare arrows). On a
phone the trio lays out as narrow arrow, wide label, narrow arrow. The
Schedule's bulk actions (Copy last week, Clear week) sit in their own
`.week-actions` group with a divider, so the destructive one no longer sits
shoulder to shoulder with the arrows; on a phone they take their own row.

## Smaller tidy-ups that came with it

- `/project`: CSV and Excel folded into one **⬇ Download ▾** menu
  (`details.btnmenu`) beside the primary Export/Invoice button.
- `/absences`: the log form is a folded card (`details.fold`) that opens
  itself after a save or a refusal; the who's-away dashboard leads the page.
- `base.html`'s navigation progress bar now skips `.xlsx` and `.pdf` links as
  well as `.csv`, since a download never fires `pageshow` to stop it.
- Labels: "Weekly grid" → **My week** (its heading was already "Your week").
