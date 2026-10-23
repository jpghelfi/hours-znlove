# Tools catalog (`/tools`)

A list of the software the team uses (Slack, Google account, Claude…) and who
has each. Everyone can read it; admins create, edit, retire and assign.

## Storage

One Notion database, **Tools** (`src/setup_tools_db.py`; `TOOLS_DB_ID` /
`TOOLS_DS_ID` on Render, `tools_*` in `databases.json` locally):

| Property | Type | Notes |
|---|---|---|
| `Tool` | title | the name; a second *active* tool with the same name (case/punctuation-blind, `_norm`) is refused |
| `Category` | text | free text, not a select — a select invents options for any name it's handed. The dialog suggests categories already in use (`<datalist>`) |
| `URL` | url | `slack.com` gains `https://` |
| `Notes` | text | plan, admin, how to get access |
| `People` | people | **the assignments** |
| `Active` | checkbox | unticked = retired; the row and its holders stay on record |

Assignments are a people property on the tool row, not a database of their
own — the same shape as a project's `People`. An assignment says nothing beyond
"this person has this tool", so the whole catalog including who-has-what is one
query (`ops.list_tools`).

## Page

- **Catalog** — one row per tool: link, category, notes, holders. Admins get
  `＋ New tool` / `Edit` (one shared dialog, which also holds *Retire*), a
  `＋ add…` picker per row, and click a holder's name to remove them (confirmed).
- **By person** — roster × tools checkbox matrix, the fast way to set someone
  up. Saves per click, totals update in place.
- **Your tools** strip at the top for whoever is looking. Non-admins get the
  same two views read-only, reached from the avatar menu ("My tools").
- Retired tools fold into a `Retired (n)` list with *Restore*.

## Writes

`POST /api/tools/create`, `/api/tools/update` (`field` = name · category · url
· notes · active), `/api/tools/assign` (`tool_id`, `user_id`, `on`). All admin +
same-origin (`_people_write_guard`). Tool ids come from the browser, so every
write retrieves the page and refuses one whose parent isn't the Tools data
source. `assign_tool` is read-modify-write of the whole `People` array (Notion
can't add one person), so it runs under `_write_lock`; giving a tool requires an
active roster member, taking one away doesn't (so a former member can be
cleaned off). Holders who left the roster show greyed as "former member".

Until the database is configured the page says so and nothing else breaks
(`tools_enabled()`). Tests: `tests/test_tools.py` (no Notion).
