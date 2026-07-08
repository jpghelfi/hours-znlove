# Hours Tracker — Notion Setup

Manual time entry: each person selects themselves, a project, a date, logs hours + a description.
Built **natively in Notion** — no code, no API token, no hosting.

---

## 1. Create the "Time Entries" database

Create a new **Table** database called **`Time Entries`** with these properties:

| Property      | Type            | Notes |
|---------------|-----------------|-------|
| `Entry`       | Title           | Auto-label; can be person + date, or just leave blank. |
| `Person`      | **Person**      | Notion's native Person type — users pick themselves from workspace members. |
| `Project`     | **Relation**    | Relates to the `Projects` database (see step 2). |
| `Date`        | **Date**        | The day the work happened. |
| `Hours`       | **Number**      | Format as *Number*; decimal hours (e.g. `1.5`). |
| `Description` | **Text**        | What was done. |
| `Week`        | Formula (opt.)  | `formatDate(prop("Date"), "YYYY-[W]ww")` — handy for weekly grouping. |

> **Why `Person` type, not a Select?** The Person type ties entries to real Notion accounts,
> so "select themselves" is one click and you can't typo a name. It also powers per-person filtered views.

## 2. Create a "Projects" database

A tiny database called **`Projects`**:

| Property   | Type   |
|------------|--------|
| `Name`     | Title  |
| `Active`   | Checkbox (filter forms to active projects) |
| `Client`   | Text / Select (optional) |

Add your projects as rows. Then in `Time Entries`, point the `Project` **Relation** at this database.

> Using a **Relation** (instead of a Select) means projects live in one place, can carry
> their own budgets/clients, and roll up total hours automatically (step 4).

## 3. Add a Form for entry

In the `Time Entries` database: **New view → Form**.

- Include fields: **Person, Project, Date, Hours, Description**.
- Set **Date** default to "Today".
- Optionally filter the Project field's options to `Active = true`.
- Share the form link with the team — they fill it out; each submission becomes a row.

This is your "manual entry" flow. Works on mobile too.

## 4. Reporting views (no code)

On `Time Entries`, add views:

- **By Person** — Board or Table grouped by `Person`; sub-group/sum `Hours`.
- **This Week** — Table filtered `Date is within → This week`, grouped by `Person`.
- **By Project** — grouped by `Project`, showing summed `Hours`.

On `Projects`, add a **Rollup**: `Total Hours = Rollup(Time Entries → Hours → Sum)` to see
hours per project at a glance.

---

## When to add code later (not now)

Reach for a Python layer only when Notion natively can't do it:

- **Import** hours from Harvest / Google Calendar / a CSV into Notion automatically.
- **Custom reports** (e.g. billing exports, cross-project analytics Notion rollups can't express).
- **Scheduled digests** (e.g. weekly Slack summary of logged hours).

At that point the setup is: create a Notion **internal integration**, share the two databases
with it, and use the Notion API (`notion-client` in Python) to read/write. The schema above is
already API-friendly. This repo (`hours-znlove`) is where that code would live.
