"""Creating the report as a real Google Sheet, named and filled.

Google has no URL that makes a spreadsheet holding your data, so the clipboard
button on the export screen can only open a blank sheet to paste into. With the
Sheets API — the same OAuth the Gmail send uses — the app can create the file
itself, title it "<Project> - Hours - <Period>", and hand back its URL.

Layout mirrors report_xlsx (same `group()`, so the workbook and the sheet can't
disagree): a Summary tab when more than one project is in scope, then one tab
per project with a by-person table and the full log.
"""
from __future__ import annotations

from . import report_xlsx
from .google_auth import call

CREATE_URL = "https://sheets.googleapis.com/v4/spreadsheets"


def _project_tab(g: dict, period_label: str) -> list[list]:
    n = len(g["people"])
    rows: list[list] = [
        [g["name"]],
        [f"{period_label} · {g['hours']:g} h · {n} {'person' if n == 1 else 'people'} · "
         f"{g['entries']} {'entry' if g['entries'] == 1 else 'entries'}"],
        [],
        ["By person"],
        ["Person", "Hours", "Days", "Entries"],
    ]
    rows += [[p["name"], p["hours"], p["days"], p["entries"]] for p in g["people"]]
    rows += [["Total", g["hours"]], [], ["Log"],
             ["Date", "Person", "Hours", "Comment", "Ticket"]]
    rows += [[e["date"], e["person"], e["hours"], e["description"], _ticket(e)]
             for e in g["log"]]
    return rows


def _ticket(entry: dict) -> str:
    """The linked Notion ticket as a clickable cell (the values write goes in as
    USER_ENTERED, so a formula lands as a formula). Quotes in a ticket name
    would end the string early, so they're doubled."""
    url = entry.get("task_url")
    if not url:
        return ""
    label = (entry.get("task") or "Notion ticket").replace('"', '""')
    return f'=HYPERLINK("{url}","{label}")'


def _summary_tab(groups: list[dict], period_label: str) -> list[list]:
    rows: list[list] = [
        ["Hours by project"], [period_label], [],
        ["Project", "Hours", "People", "Days", "Entries"],
    ]
    rows += [[g["name"], g["hours"], len(g["people"]), g["days"], g["entries"]]
             for g in groups]
    rows += [["Total", round(sum(g["hours"] for g in groups), 2), "", "",
              sum(g["entries"] for g in groups)]]
    return rows


def _bold(sheet_id: int, row: int) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": row, "endRowIndex": row + 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold",
    }}


def create(entries: list[dict], period_label: str, label: str) -> dict:
    """Create the spreadsheet and return {"url", "title", "tabs"}.

    Two calls rather than one giant create payload: the create names the file
    and its tabs, the values update fills them. Easier to read, and a failure
    tells you which half went wrong.
    """
    groups = report_xlsx.group(entries)
    title = f"{label} - Hours - {period_label}"
    used: set = set()
    tabs = []
    if len(groups) > 1:
        tabs.append(("Summary", _summary_tab(groups, period_label)))
        used.add("summary")
    for g in groups:
        tabs.append((report_xlsx.sheet_title(g["name"], used), _project_tab(g, period_label)))
    if not tabs:  # an empty period still gets a file, so the link always works
        tabs.append(("No entries", [[f"No hours logged · {label}"], [period_label]]))

    made = call("POST", CREATE_URL, json={
        "properties": {"title": title},
        "sheets": [{"properties": {"title": name}} for name, _ in tabs],
    }).json()

    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                 for s in made["sheets"]}
    call("POST", f"{CREATE_URL}/{made['spreadsheetId']}/values:batchUpdate", json={
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": f"'{name}'!A1", "values": rows} for name, rows in tabs],
    })

    # bold the title row and each table's header, and let the columns fit
    requests: list[dict] = []
    for name, rows in tabs:
        sid = sheet_ids[name]
        requests.append(_bold(sid, 0))
        for i, row in enumerate(rows):
            if row and row[0] in ("Person", "Date", "Project", "By person", "Log", "Total"):
                requests.append(_bold(sid, i))
        requests.append({"autoResizeDimensions": {"dimensions": {
            "sheetId": sid, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": 6}}})
    if requests:
        call("POST", f"{CREATE_URL}/{made['spreadsheetId']}:batchUpdate",
             json={"requests": requests})

    return {"url": made.get("spreadsheetUrl", ""), "title": title, "tabs": len(tabs)}
