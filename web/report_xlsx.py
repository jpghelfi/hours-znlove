"""The per-project Excel export: one sheet per project, people then their log.

Kept out of app.py (which holds routes/HTTP concerns) and out of notion_ops.py
(which holds Notion reads/writes): this module only turns entries that have
already been read into a workbook.

Shape, per project sheet: a title block, a "by person" table (hours, days,
entries), then every entry with its date, person and comment — the thing you
hand to a client or a PM, rather than the flat row dump /project.csv gives.
"""
from __future__ import annotations

import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

_INK = "1B2138"
_MUTED = "6B7280"
_HEAD_FILL = PatternFill("solid", fgColor="EEF2F9")
_TITLE_FONT = Font(bold=True, size=15, color=_INK)
_SUB_FONT = Font(size=10, color=_MUTED)
_SECTION_FONT = Font(bold=True, size=11, color=_INK)
_TH_FONT = Font(bold=True, size=10, color=_INK)
_TOTAL_FONT = Font(bold=True, size=10, color=_INK)
_RULE = Border(bottom=Side(style="thin", color="D6DBE6"))
_HOURS_FMT = "0.##"

# Excel forbids these in a sheet name, and caps it at 31 chars
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _sheet_title(name: str, used: set) -> str:
    """A legal, unique sheet name for a project. Long project names collide once
    truncated to 31 chars, so uniqueness is enforced with a numeric suffix."""
    base = _BAD_SHEET_CHARS.sub(" ", name).strip() or "Project"
    title = base[:31]
    n = 2
    while title.lower() in used:
        suffix = f" ({n})"
        title = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(title.lower())
    return title


def _widths(ws, widths: dict) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def _group(entries: list[dict]) -> list[dict]:
    """entries -> one record per project, each with its people and its log.

    Grouped by project *id* (two projects may share a name), ordered by hours
    desc so the busiest project is the first sheet; people inside a project are
    ordered the same way.
    """
    groups: dict = {}
    for e in entries:
        g = groups.setdefault(e.get("project_id") or e.get("project") or "(none)", {
            "name": e.get("project") or "(none)", "hours": 0.0, "people": {}, "log": [],
        })
        g["hours"] += e["hours"]
        g["log"].append(e)
        p = g["people"].setdefault(e.get("person_id") or e["person"], {
            "name": e["person"], "hours": 0.0, "entries": 0, "days": set(),
        })
        p["hours"] += e["hours"]
        p["entries"] += 1
        p["days"].add(e["date"])
    out = []
    for g in sorted(groups.values(), key=lambda g: (-g["hours"], g["name"].lower())):
        out.append({
            "name": g["name"],
            "hours": round(g["hours"], 2),
            "entries": len(g["log"]),
            "days": len({e["date"] for e in g["log"]}),
            "people": sorted(
                ({"name": p["name"], "hours": round(p["hours"], 2),
                  "entries": p["entries"], "days": len(p["days"])}
                 for p in g["people"].values()),
                key=lambda p: (-p["hours"], p["name"].lower())),
            # chronological: the sheet reads as a work log, oldest first
            "log": sorted(g["log"], key=lambda e: (e["date"], e["person"].lower())),
        })
    return out


def _summary_sheet(wb: Workbook, groups: list[dict], period_label: str) -> None:
    ws = wb.create_sheet("Summary")
    ws["A1"] = "Hours by project"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = period_label
    ws["A2"].font = _SUB_FONT
    headers = ["Project", "Hours", "People", "Days", "Entries"]
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=4, column=i, value=h)
        c.font, c.fill, c.border = _TH_FONT, _HEAD_FILL, _RULE
    row = 5
    for g in groups:
        ws.cell(row=row, column=1, value=g["name"])
        ws.cell(row=row, column=2, value=g["hours"]).number_format = _HOURS_FMT
        ws.cell(row=row, column=3, value=len(g["people"]))
        ws.cell(row=row, column=4, value=g["days"])
        ws.cell(row=row, column=5, value=g["entries"])
        row += 1
    ws.cell(row=row, column=1, value="Total").font = _TOTAL_FONT
    total = ws.cell(row=row, column=2, value=round(sum(g["hours"] for g in groups), 2))
    total.font, total.number_format = _TOTAL_FONT, _HOURS_FMT
    ws.cell(row=row, column=5, value=sum(g["entries"] for g in groups)).font = _TOTAL_FONT
    _widths(ws, {"A": 42, "B": 10, "C": 10, "D": 8, "E": 10})
    ws.freeze_panes = "A5"


def _project_sheet(wb: Workbook, g: dict, period_label: str, used: set) -> None:
    ws = wb.create_sheet(_sheet_title(g["name"], used))
    ws["A1"] = g["name"]
    ws["A1"].font = _TITLE_FONT
    n = len(g["people"])
    ws["A2"] = (f"{period_label} · {g['hours']:g} h · {n} {'person' if n == 1 else 'people'} · "
                f"{g['entries']} {'entry' if g['entries'] == 1 else 'entries'}")
    ws["A2"].font = _SUB_FONT

    ws["A4"] = "By person"
    ws["A4"].font = _SECTION_FONT
    for i, h in enumerate(["Person", "Hours", "Days", "Entries"], start=1):
        c = ws.cell(row=5, column=i, value=h)
        c.font, c.fill, c.border = _TH_FONT, _HEAD_FILL, _RULE
    row = 6
    for p in g["people"]:
        ws.cell(row=row, column=1, value=p["name"])
        ws.cell(row=row, column=2, value=p["hours"]).number_format = _HOURS_FMT
        ws.cell(row=row, column=3, value=p["days"])
        ws.cell(row=row, column=4, value=p["entries"])
        row += 1
    ws.cell(row=row, column=1, value="Total").font = _TOTAL_FONT
    tot = ws.cell(row=row, column=2, value=g["hours"])
    tot.font, tot.number_format = _TOTAL_FONT, _HOURS_FMT

    log_head = row + 3
    ws.cell(row=log_head - 1, column=1, value="Log").font = _SECTION_FONT
    for i, h in enumerate(["Date", "Person", "Hours", "Comment"], start=1):
        c = ws.cell(row=log_head, column=i, value=h)
        c.font, c.fill, c.border = _TH_FONT, _HEAD_FILL, _RULE
    row = log_head + 1
    for e in g["log"]:
        ws.cell(row=row, column=1, value=e["date"])
        ws.cell(row=row, column=2, value=e["person"])
        ws.cell(row=row, column=3, value=e["hours"]).number_format = _HOURS_FMT
        c = ws.cell(row=row, column=4, value=e["description"])
        # comments are free text and often long: wrap rather than run off the page
        c.alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    if g["log"]:
        ws.auto_filter.ref = f"A{log_head}:D{row - 1}"
    _widths(ws, {"A": 12, "B": 24, "C": 9, "D": 90})
    # keep the log's header visible while scrolling a long month
    ws.freeze_panes = f"A{log_head + 1}"


def build(entries: list[dict], period_label: str, scope_label: str) -> bytes:
    """The workbook as bytes: a Summary sheet (when more than one project) plus
    one sheet per project. An empty period still yields a valid one-sheet file —
    Excel refuses to open a workbook with no sheets."""
    groups = _group(entries)
    wb = Workbook()
    wb.remove(wb.active)  # drop the default sheet; every sheet here is named
    if not groups:
        ws = wb.create_sheet("No entries")
        ws["A1"] = f"No hours logged · {scope_label}"
        ws["A1"].font = _TITLE_FONT
        ws["A2"] = period_label
        ws["A2"].font = _SUB_FONT
        _widths(ws, {"A": 60})
    else:
        if len(groups) > 1:
            _summary_sheet(wb, groups, period_label)
        used: set = set()
        for g in groups:
            _project_sheet(wb, g, period_label, used)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
