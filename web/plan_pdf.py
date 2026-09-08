"""A project's plan as a PDF — the timeline drawn as bars, then the items.

Like invoice_pdf.py this module only turns rows already read into bytes; it
never touches Notion. It is called twice over: by /plan.pdf with the full
rows an admin sees, and by /p/<token>.pdf with the **stripped** rows the
share page shows — one builder, so the two can't disagree about what a
column means. Which columns are drawn is decided by the rows themselves
(`show_people`, `show_hours` on the context), not by who asked.

Landscape A4. The timeline is a day-per-column strip over the period —
weekdays only, since the planner never books a weekend — with a filled
rectangle per item (colour by status, filled to tracked ÷ estimate when
hours are shown), a diamond for a milestone and a rule for today. Below it,
one table row per item. A month is 22 weekday columns at ~11 mm, which fits
beside a 60 mm name column; a week gets wider columns, not more of them.

Rows overflow one page gracefully in the table (platypus continues it) but
the timeline deliberately does not shrink to unreadable: it draws the first
`MAX_BARS` items and says how many it left out.
"""
from __future__ import annotations

import datetime as dt
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

INK = colors.HexColor("#1B2138")
MUTED = colors.HexColor("#6B7280")
RULE = colors.HexColor("#D6DBE6")
HEAD_BG = colors.HexColor("#EEF2F9")
TODAY = colors.HexColor("#C93B54")
WEEK_BG = colors.HexColor("#F7F9FC")

# status -> (fill, ink). The same hues the page uses, chosen to read in print.
STATUS_COLORS = {
    "Backlog": ("#E5E7EB", "#4B5563"),
    "Planned": ("#DCE9FB", "#1A4E8A"),
    "In progress": ("#FBEAD2", "#8A5411"),
    "Blocked": ("#FBE0E6", "#9E2038"),
    "Done": ("#D6F2EA", "#0A6B57"),
    "Dropped": ("#F3F4F6", "#9CA3AF"),
}

_MARGIN = 14 * mm
MAX_BARS = 40
_NAME_W = 62 * mm
_ROW_H = 7.2 * mm
_HEAD_H = 9 * mm


def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold",
                                fontSize=17, leading=21, textColor=INK, alignment=0,
                                spaceAfter=0),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=9.5, leading=12,
                              textColor=MUTED),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=8.5, leading=10.5,
                               textColor=INK),
        "muted": ParagraphStyle("m", parent=base["Normal"], fontSize=8.5, leading=10.5,
                                textColor=MUTED),
        "h": ParagraphStyle("h", parent=base["Normal"], fontName="Helvetica-Bold",
                            fontSize=8, leading=10, textColor=MUTED),
    }


def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _human(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return dt.date.fromisoformat(iso[:10]).strftime("%d %b")
    except ValueError:
        return iso


def _hours(v) -> str:
    if v is None:
        return "—"
    return f"{v:g}"


def weekday_columns(date_from: str, date_to: str) -> list[dt.date]:
    a, b = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


class Timeline(Flowable):
    """The bars. Drawn straight on the canvas: a table can't paint a bar that
    spans a fraction of a cell, and this is one rectangle per row anyway."""

    def __init__(self, items: list[dict], cols: list[dt.date], width: float,
                 show_hours: bool, today: dt.date | None):
        super().__init__()
        self.items = items[:MAX_BARS]
        self.left_out = max(0, len(items) - MAX_BARS)
        self.cols = cols
        self.width = width
        self.show_hours = show_hours
        self.today = today
        self.height = _HEAD_H + _ROW_H * len(self.items) + (5 * mm if self.left_out else 0)

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c = self.canv
        cols = self.cols
        n = max(1, len(cols))
        grid_w = self.width - _NAME_W
        cw = grid_w / n
        top = self.height
        # header: day numbers, month labels, weekly bands
        c.setFillColor(HEAD_BG)
        c.rect(0, top - _HEAD_H, self.width, _HEAD_H, stroke=0, fill=1)
        c.setFont("Helvetica-Bold", 7.5)
        c.setFillColor(MUTED)
        c.drawString(2 * mm, top - _HEAD_H + 3 * mm, "Item")
        idx = {d: i for i, d in enumerate(cols)}
        for i, d in enumerate(cols):
            x = _NAME_W + i * cw
            if d.weekday() == 0 and i:
                c.setStrokeColor(RULE)
                c.setLineWidth(0.6)
                c.line(x, 0, x, top)
            c.setFillColor(MUTED)
            c.setFont("Helvetica-Bold" if d == self.today else "Helvetica", 7)
            label = d.strftime("%d") if n > 8 else d.strftime("%a %d")
            c.drawCentredString(x + cw / 2, top - _HEAD_H + 3 * mm, label)
            if d.day == 1 or i == 0:
                c.setFont("Helvetica-Bold", 6.5)
                c.drawString(x + 1, top - 2.6 * mm, d.strftime("%b"))
        # rows
        y = top - _HEAD_H
        for r in self.items:
            y -= _ROW_H
            c.setStrokeColor(RULE)
            c.setLineWidth(0.4)
            c.line(0, y, self.width, y)
            c.setFillColor(INK)
            c.setFont("Helvetica", 8)
            name = r["name"]
            while c.stringWidth(name, "Helvetica", 8) > _NAME_W - 4 * mm and len(name) > 4:
                name = name[:-2]
            if name != r["name"]:
                name = name.rstrip() + "…"
            c.drawString(2 * mm, y + 2.4 * mm, name)
            if not r.get("start"):
                c.setFillColor(MUTED)
                c.setFont("Helvetica-Oblique", 7)
                c.drawString(_NAME_W + 1.5 * mm, y + 2.4 * mm, "unscheduled")
                continue
            s = dt.date.fromisoformat(r["start"])
            e = dt.date.fromisoformat(r["end"] or r["start"])
            # clip to the columns on screen; a bar past the edge gets a notch
            first_i = next((idx[d] for d in cols if d >= s), None)
            last_i = next((idx[d] for d in reversed(cols) if d <= e), None)
            if first_i is None or last_i is None or first_i > last_i:
                continue
            fill, ink = STATUS_COLORS.get(r["status"], STATUS_COLORS["Backlog"])
            x0 = _NAME_W + first_i * cw + 0.6
            x1 = _NAME_W + (last_i + 1) * cw - 0.6
            if r.get("type") == "Milestone":
                cx, cy, h = (x0 + x1) / 2, y + _ROW_H / 2, 2.4 * mm
                c.setFillColor(colors.HexColor(ink))
                p = c.beginPath()
                p.moveTo(cx, cy + h); p.lineTo(cx + h, cy); p.lineTo(cx, cy - h); p.lineTo(cx - h, cy)
                p.close()
                c.drawPath(p, stroke=0, fill=1)
                continue
            bh = _ROW_H - 2.4 * mm
            by = y + 1.2 * mm
            c.setFillColor(colors.HexColor(fill))
            c.roundRect(x0, by, x1 - x0, bh, 1.2 * mm, stroke=0, fill=1)
            if self.show_hours and r.get("estimate") and r.get("tracked") is not None:
                pct = min(1.0, (r["tracked"] or 0) / r["estimate"])
                c.setFillColor(colors.HexColor(ink))
                c.setFillAlpha(0.28)
                c.roundRect(x0, by, (x1 - x0) * pct, bh, 1.2 * mm, stroke=0, fill=1)
                c.setFillAlpha(1)
            if s < cols[0]:
                c.setFillColor(colors.HexColor(ink))
                c.rect(x0, by, 0.8 * mm, bh, stroke=0, fill=1)
            if e > cols[-1]:
                c.setFillColor(colors.HexColor(ink))
                c.rect(x1 - 0.8 * mm, by, 0.8 * mm, bh, stroke=0, fill=1)
            # a label inside the bar when it fits
            c.setFillColor(colors.HexColor(ink))
            c.setFont("Helvetica-Bold", 6.5)
            label = r["status"]
            if self.show_hours and r.get("estimate") is not None:
                label += f" · {_hours(r.get('tracked') or 0)}/{_hours(r['estimate'])} h"
            if c.stringWidth(label, "Helvetica-Bold", 6.5) < (x1 - x0) - 2 * mm:
                c.drawString(x0 + 1 * mm, by + 1.6 * mm, label)
        if self.left_out:
            c.setFillColor(MUTED)
            c.setFont("Helvetica-Oblique", 7.5)
            c.drawString(2 * mm, 1.2 * mm, f"and {self.left_out} more — see the list below")
        # today
        if self.today in idx:
            x = _NAME_W + idx[self.today] * cw + cw / 2
            c.setStrokeColor(TODAY)
            c.setLineWidth(0.9)
            c.line(x, 0, x, top - _HEAD_H)   # under the header, not through its labels


def _items_table(items: list[dict], st: dict, width: float, show_people: bool,
                 show_hours: bool) -> Table:
    head = ["Item", "Type", "Status", "Dates"]
    if show_people:
        head.append("Owner")
    if show_hours:
        head += ["Est. h", "Tracked h"]
    rows = [[Paragraph(_esc(h), st["h"]) for h in head]]
    for r in items:
        dates = ("unscheduled" if not r.get("start") else
                 _human(r["start"]) if r["start"] == r.get("end") else
                 f"{_human(r['start'])} – {_human(r['end'])}")
        line = [Paragraph(_esc(r["name"]), st["body"]),
                Paragraph(_esc(r.get("type") or ""), st["muted"]),
                Paragraph(_esc(r.get("status") or ""), st["body"]),
                Paragraph(_esc(dates), st["muted"])]
        if show_people:
            line.append(Paragraph(_esc(r.get("owner") or "—"), st["muted"]))
        if show_hours:
            line += [Paragraph(_hours(r.get("estimate")), st["body"]),
                     Paragraph(_hours(r.get("tracked")) if r.get("tracked") is not None else "—",
                               st["body"])]
        rows.append(line)
    fixed = [26 * mm, 26 * mm, 34 * mm] + ([32 * mm] if show_people else []) \
        + ([18 * mm, 20 * mm] if show_hours else [])
    widths = [width - sum(fixed)] + fixed
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def filename(project_name: str, period_label: str) -> str:
    import re
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{project_name} plan {period_label}").strip("-")
    return f"{slug or 'plan'}.pdf"


def build(ctx: dict, items: list[dict]) -> bytes:
    """`ctx`: project (name), period_label, date_from, date_to, generated (iso
    date), show_people, show_hours, optional update (a paragraph), pm/am names.
    `items`: the plan rows, already stripped for the audience."""
    st = _styles()
    cols = weekday_columns(ctx["date_from"], ctx["date_to"])
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4), leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=_MARGIN,
        title=f"{ctx.get('project') or ''} — plan — {ctx.get('period_label') or ''}".strip(" —"),
        author=ctx.get("company") or "", subject=ctx.get("period_label") or "")
    width = doc.width
    story: list = []
    story.append(Paragraph(_esc(ctx.get("project") or "Plan"), st["title"]))
    bits = [ctx.get("period_label") or ""]
    who = [f"{k}: {v}" for k, v in (("PM", ctx.get("pm")), ("Account manager", ctx.get("am"))) if v]
    if ctx.get("show_people") and who:
        bits.append(" · ".join(who))
    done = sum(1 for r in items if r.get("status") == "Done")
    live = [r for r in items if r.get("status") != "Dropped"]
    if live:
        bits.append(f"{done} of {len(live)} done")
    if ctx.get("show_hours"):
        est = sum(r.get("estimate") or 0 for r in live)
        trk = sum(r.get("tracked") or 0 for r in live if r.get("tracked") is not None)
        if est:
            bits.append(f"{trk:g} h tracked of {est:g} h estimated")
    bits.append(f"as of {_human(ctx.get('generated'))} {dt.date.fromisoformat(ctx['generated']).year}"
                if ctx.get("generated") else "")
    story.append(Paragraph(_esc(" · ".join(b for b in bits if b)), st["sub"]))
    if ctx.get("update"):
        story.append(Spacer(1, 4))
        story.append(Paragraph(_esc(ctx["update"]), st["body"]))
    story.append(Spacer(1, 8))
    today = dt.date.fromisoformat(ctx["generated"]) if ctx.get("generated") else None
    story.append(Timeline(items, cols, width, bool(ctx.get("show_hours")), today))
    if items:
        story.append(PageBreak())
        story.append(Paragraph("Items", st["h"]))
        story.append(Spacer(1, 4))
        story.append(_items_table(items, st, width, bool(ctx.get("show_people")),
                                  bool(ctx.get("show_hours"))))
    else:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Nothing planned for this period yet.", st["muted"]))
    doc.build(story)
    return buf.getvalue()
