"""The arithmetic behind /dashboard, kept free of Notion so it can be tested.

Everything here takes plain lists and dicts (what the routes already read) and
returns rows for the template. `app.py` does the reads and the glue.

One rule runs through all of it: a weekday counts as *elapsed* only once it's
over — the 1st through yesterday. Today is still being logged, so counting it
would understate everyone's pace and flag everyone who hasn't logged yet.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

UTIL_OVER = 110   # % of capacity above which a person reads as over-booked
UTIL_FULL = 90
UTIL_LOW = 60


def elapsed_weekdays(days: list, today: dt.date) -> list:
    """The weekdays in `days` that are already over (strictly before today)."""
    return [d for d in days if d < today]


def forecast(used: float, elapsed: int, total: int) -> Optional[float]:
    """Where a month ends at its current pace. None when there's no pace yet."""
    if total <= 0 or elapsed <= 0:
        return None
    if elapsed >= total:
        return used
    return used / elapsed * total


def add_forecast(rows: list, elapsed: int, total: int,
                 used_to_date: Optional[dict] = None) -> list:
    """Lay a forecast over /budgets' rows (order kept: they're trouble-first).

    The pace is hours logged on *elapsed* days only (`used_to_date`, by project
    id), so today's half-logged hours don't inflate it — the same window the
    denominator counts. Without the map, a row's whole `tracked` is used."""
    for r in rows:
        used = r["tracked"] if used_to_date is None else used_to_date.get(r["id"], 0.0)
        f = forecast(used, elapsed, total)
        if f is not None and elapsed < total:
            f = max(f, r["tracked"])   # never forecast below what's already logged
        b = r.get("budget")
        r["forecast"] = f
        cap = b["hours"] if b else None
        r["forecast_pct"] = (f / cap * 100) if (f is not None and cap) else None
        r["forecast_over"] = bool(b and f is not None and f > cap)
    return rows


def day_cap(person: Optional[dict], default: float) -> float:
    """A person's hours a day; their own value if set, otherwise the default."""
    return (person or {}).get("hours_per_day") or default


def week_cap(person: Optional[dict], default_day: float, override: Optional[float]) -> float:
    """A person's week. Their own hours/day always wins; WEEK_TARGET_HOURS
    (`override`) only replaces the default for people without one."""
    hpd = (person or {}).get("hours_per_day")
    if hpd:
        return hpd * 5
    return override if override is not None else default_day * 5


def person_capacity(hpd: float, workdays: int, absent: int) -> float:
    return max(0, workdays - absent) * hpd


def _util_status(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct > UTIL_OVER:
        return "over"
    if pct >= UTIL_FULL:
        return "full"
    if pct < UTIL_LOW:
        return "low"
    return "ok"


def utilization_rows(people: list, hours_by_person: dict, absent_by_person: dict,
                     elapsed: list, all_days: list, default: float) -> list:
    """One row per roster person: logged hours against capacity *to date*
    (elapsed weekdays minus approved absence days), with the whole month's
    capacity alongside."""
    elapsed_set, month_set = set(elapsed), set(all_days)
    rows = []
    for p in people:
        hpd = day_cap(p, default)
        absent = absent_by_person.get(p["id"], set())
        absent_to_date = len(absent & elapsed_set)
        absent_month = len(absent & month_set)
        cap_to_date = person_capacity(hpd, len(elapsed), absent_to_date)
        used = hours_by_person.get(p["id"], 0.0)
        pct = (used / cap_to_date * 100) if cap_to_date else None
        rows.append({
            "id": p["id"], "name": p["name"], "hpd": hpd, "custom": bool(p.get("hours_per_day")),
            "used": used, "absent_to_date": absent_to_date, "absent_month": absent_month,
            "cap_to_date": cap_to_date,
            "cap_month": person_capacity(hpd, len(all_days), absent_month),
            "pct": pct, "status": _util_status(pct),
        })
    rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0), r["name"].lower()))
    return rows


def missing_days(elapsed: list, logged: set, absent: set) -> list:
    """Elapsed weekdays with nothing logged and no approved absence."""
    return [d for d in elapsed if d not in logged and d not in absent]


def missing_rows(people: list, logged_by_person: dict, absent_by_person: dict,
                 elapsed: list) -> list:
    """Everyone with at least one unexplained empty weekday, worst first."""
    rows = []
    for p in people:
        days = missing_days(elapsed, logged_by_person.get(p["id"], set()),
                            absent_by_person.get(p["id"], set()))
        if days:
            rows.append({"id": p["id"], "name": p["name"], "days": days, "n": len(days)})
    rows.sort(key=lambda r: (-r["n"], r["name"].lower()))
    return rows


def pipeline_rows(projects: list, tracked_by_id: dict, invoices_by_key: dict,
                  month_iso: str) -> list:
    """Per project with hours (or an invoice) this month: is it invoiced, and
    what's it roughly worth at the project's rate. Uninvoiced first."""
    rows = []
    for p in projects:
        hours = tracked_by_id.get(p["id"], 0.0)
        inv = invoices_by_key.get((p["id"], month_iso))
        if not hours and not inv:
            continue
        billing = p.get("billing") or {}
        rate = billing.get("rate")
        rows.append({
            "id": p["id"], "name": p["name"], "hours": hours, "invoice": inv,
            "status": (inv or {}).get("status") or "none",
            "rate": rate, "currency": billing.get("currency") or "",
            "value": hours * rate if rate else None,
        })
    rows.sort(key=lambda r: (r["invoice"] is not None, -r["hours"], r["name"].lower()))
    return rows
