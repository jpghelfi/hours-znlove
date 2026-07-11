"""FastAPI web app for the Notion hours tracker.

Notion is the source of truth; this app is a nicer entry form and an editable
Mon–Fri weekly grid on top of it, behind Notion OAuth login (allowlisted).

Run:  ./.venv/bin/uvicorn web.app:app --reload --port 8000
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from . import notion_ops as ops

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="Hours Tracker")

# P0: never run with a guessable session secret — forged cookies = forged admins.
_secret = os.environ.get("SESSION_SECRET")
if not _secret:
    if os.environ.get("AUTH_DISABLED") == "1":
        _secret = "dev-insecure-secret"  # local dev only; login is bypassed anyway
    else:
        raise RuntimeError("SESSION_SECRET must be set (refusing to start with a default secret).")
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret,
    max_age=60 * 60 * 24 * 30,   # 30 days — stay logged in, so the Notion consent
    same_site="lax",              # is only hit on rare re-logins, not every visit
    https_only=os.environ.get("AUTH_DISABLED") != "1",  # Secure cookie in production
)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    ops.ensure_person_property()
    _start_keepalive()


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


# Render's free tier spins the instance down after 15 min without inbound
# traffic; requesting our own public URL counts as traffic, so a 10-minute
# self-ping keeps it awake. Render sets RENDER_EXTERNAL_URL automatically —
# absent locally, so dev servers don't ping. KEEPALIVE_MINUTES=0 disables.
def _start_keepalive() -> None:
    base_url = os.environ.get("RENDER_EXTERNAL_URL")
    minutes = float(os.environ.get("KEEPALIVE_MINUTES", "10"))
    if not base_url or minutes <= 0:
        return

    def _ping_forever() -> None:
        import time
        import urllib.request
        url = base_url.rstrip("/") + "/healthz"
        while True:
            time.sleep(minutes * 60)
            try:
                urllib.request.urlopen(url, timeout=30).read()
            except Exception:
                pass  # transient failure — the next ping is 10 min away anyway

    threading.Thread(target=_ping_forever, name="keepalive", daemon=True).start()


# ---- auth helpers ------------------------------------------------------

def current_user(request: Request) -> Optional[dict]:
    if auth.auth_disabled():
        # Local dev: act as a real person so per-user filtering works. Set
        # DEV_USER_ID (a Notion user id) in .env to see that person's hours.
        return {
            "id": os.environ.get("DEV_USER_ID") or None,
            "name": os.environ.get("DEV_USER_NAME", "Dev User"),
            "email": os.environ.get("DEV_USER_EMAIL", "dev@local"),
        }
    return request.session.get("user")


def _require_login(request: Request) -> Optional[dict]:
    """Return the user dict, or None if the caller should be redirected to login."""
    return current_user(request)


def _same_origin(request: Request) -> bool:
    """CSRF guard for state-changing POSTs: browser requests must come from us."""
    from urllib.parse import urlparse
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # non-browser clients (curl) send neither
    return urlparse(origin).netloc == request.headers.get("host")


def _parse_date(s: Optional[str]) -> Optional[dt.date]:
    """Strict ISO date or None — malformed input falls back instead of 500ing."""
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _member_project_ids(user_id: Optional[str]) -> set:
    return {p["id"] for p in ops.list_projects(member_of=user_id)}


# ---- auth routes -------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_landing(request: Request):
    if current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"denied": request.query_params.get("denied")})


@app.get("/login/start")
def login_start(request: Request):
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(url=auth.login_url(state), status_code=303)


@app.get("/auth/callback")
def auth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        return RedirectResponse(url="/login?denied=1", status_code=303)
    if not state or state != request.session.get("oauth_state"):
        return RedirectResponse(url="/login?denied=state", status_code=303)
    request.session.pop("oauth_state", None)
    try:
        user = auth.exchange_code(code)
    except Exception:
        return RedirectResponse(url="/login?denied=error", status_code=303)
    if not auth.is_allowed(user.get("email")):
        return RedirectResponse(url="/login?denied=notallowed", status_code=303)
    request.session["user"] = {"id": user["id"], "name": user["name"], "email": user["email"]}
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    # POST-only: a GET logout is trivially CSRF-able via <img src>.
    request.session.pop("user", None)
    return RedirectResponse(url="/login", status_code=303)


# ---- app pages ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def form_page(request: Request, ok: Optional[str] = None, err: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "form.html", {
        "user": user,
        "projects": ops.list_projects(member_of=user.get("id")),
        "today": dt.date.today().isoformat(),
        "ok": ok, "err": _ENTRY_ERRORS.get(err) if err else None,
    })


_ENTRY_ERRORS = {
    "date": "That date isn't valid — use the date picker.",
    "hours": "Hours must be between 0.25 and 24.",
    "project": "Pick one of your projects.",
    "save": "Couldn't save the entry — try again.",
}


@app.post("/entry")
def submit_entry(
    request: Request,
    project_id: str = Form(...),
    date: str = Form(...),
    hours: float = Form(...),
    description: str = Form(""),
):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    if not _parse_date(date) or len(date) != 10:
        return RedirectResponse(url="/?err=date", status_code=303)
    if not (0 < hours <= 24) or hours != hours:  # NaN guard
        return RedirectResponse(url="/?err=hours", status_code=303)
    if project_id not in _member_project_ids(user.get("id")):
        return RedirectResponse(url="/?err=project", status_code=303)
    try:
        ops.create_entry(user.get("id"), project_id, date, hours, description)
    except Exception:
        return RedirectResponse(url="/?err=save", status_code=303)
    return RedirectResponse(url="/?ok=1", status_code=303)


@app.get("/week", response_class=HTMLResponse)
def week_page(request: Request, monday: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    mon = ops.monday_of(_parse_date(monday))  # malformed ?monday= falls back to today
    grid = ops.week_grid(mon, user.get("id"))
    # projects logged last week but not yet on this week's grid (for "copy last week")
    prev_grid = ops.week_grid(mon - dt.timedelta(days=7), user.get("id"))
    cur_ids = {r["project_id"] for r in grid["rows"]}
    prev_projects = [{"id": r["project_id"], "name": r["project_name"]}
                     for r in prev_grid["rows"] if r["project_id"] not in cur_ids]
    target = float(os.environ.get("WEEK_TARGET_HOURS", "40"))
    return templates.TemplateResponse(request, "week.html", {
        "user": user,
        "grid": grid,
        "projects": ops.list_projects(member_of=user.get("id")),
        "prev_mon": (mon - dt.timedelta(days=7)).isoformat(),
        "next_mon": (mon + dt.timedelta(days=7)).isoformat(),
        "this_mon": ops.monday_of().isoformat(),
        "iso_week": mon.strftime("%G-W%V"),
        "prev_projects": prev_projects,
        "target": target,
        "cap_pct": min(100, round(grid["grand_total"] / target * 100)) if target else 0,
    })


@app.get("/healthz")
def healthz():
    """Cheap liveness endpoint (no auth, no Notion calls) for keep-alive pings."""
    return {"ok": True}


def _range_bounds(range_key: Optional[str], date_from: Optional[str], date_to: Optional[str]):
    today = dt.date.today()
    f, t = _parse_date(date_from), _parse_date(date_to)
    if f and t and f <= t:  # both valid or the custom range is ignored
        return f.isoformat(), t.isoformat(), "custom"
    mon = ops.monday_of(today)
    if range_key == "last-week":
        m = mon - dt.timedelta(days=7)
        return m.isoformat(), (m + dt.timedelta(days=6)).isoformat(), range_key
    if range_key == "this-month":
        return today.replace(day=1).isoformat(), today.isoformat(), range_key
    if range_key == "last-month":
        first_this = today.replace(day=1)
        last_prev = first_this - dt.timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat(), range_key
    return mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat(), "this-week"


def _report_data(user, scope, range_key, date_from, date_to):
    f, t, rk = _range_bounds(range_key, date_from, date_to)
    is_admin = auth.is_admin(user.get("email"))
    team = scope == "team" and is_admin
    entries = ops.entries_between(f, t, None if team else user.get("id"))
    total = round(sum(e["hours"] for e in entries), 2)

    def agg(key):
        d = {}
        for e in entries:
            d[e[key]] = d.get(e[key], 0) + e["hours"]
        mx = max(d.values(), default=0)
        return [{"name": k, "hours": round(v, 2), "pct": round(v / mx * 100) if mx else 0}
                for k, v in sorted(d.items(), key=lambda kv: -kv[1])]

    days = []
    d0, d1 = dt.date.fromisoformat(f), dt.date.fromisoformat(t)
    if (d1 - d0).days <= 31:
        by_day = {}
        for e in entries:
            by_day[e["date"]] = by_day.get(e["date"], 0) + e["hours"]
        mx = max(by_day.values(), default=0)
        cur = d0
        while cur <= d1:
            iso = cur.isoformat()
            v = by_day.get(iso, 0)
            days.append({"label": cur.strftime("%d"), "dow": cur.strftime("%a"),
                         "hours": round(v, 2), "pct": round(v / mx * 100) if mx else 0})
            cur += dt.timedelta(days=1)
    # planned vs actual (Forecast): allocations in range vs logged hours,
    # pivotable by project AND by person
    planned = ops.planned_rows(f, t, None if team else user.get("id"))

    def pva(dim):
        a, p = {}, {}
        for e in entries:
            a[e[dim]] = a.get(e[dim], 0) + e["hours"]
        for r in planned:
            p[r[dim]] = p.get(r[dim], 0) + r["hours"]
        names = sorted(set(a) | set(p), key=lambda n: -(a.get(n, 0) + p.get(n, 0)))
        scale = max([max(a.get(n, 0), p.get(n, 0)) for n in names], default=0) or 1
        out = []
        for n in names:
            av, pv = round(a.get(n, 0), 2), round(p.get(n, 0), 2)
            out.append({
                "name": n, "actual": av, "planned": pv,
                # both bars share one scale so lengths are comparable across rows
                "pct_a": round(av / scale * 100), "pct_p": round(pv / scale * 100),
                "delta": round(av - pv, 2),
                "done": round(av / pv * 100) if pv else None,
            })
        return out

    return {
        "from": f, "to": t, "range": rk, "team": team, "is_admin": is_admin,
        "entries": entries, "total": total,
        "by_project": agg("project"), "by_person": agg("person") if team else [],
        "days": days, "people_count": len({e["person"] for e in entries}),
        "pva": pva("project"), "pva_person": pva("person"),
    }


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, scope: str = "me", range: Optional[str] = None,
                 date_from: Optional[str] = None, date_to: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    data = _report_data(user, scope, range, date_from, date_to)
    return templates.TemplateResponse(request, "reports.html", {"user": user, "r": data, "scope": scope})


@app.get("/reports.csv")
def reports_csv(request: Request, scope: str = "me", range: Optional[str] = None,
                date_from: Optional[str] = None, date_to: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    data = _report_data(user, scope, range, date_from, date_to)
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "person", "project", "hours", "description"])
    for e in sorted(data["entries"], key=lambda e: (e["date"], e["person"])):
        w.writerow([e["date"], e["person"], e["project"], e["hours"], e["description"]])
    from fastapi.responses import Response
    fname = f"hours_{data['from']}_{data['to']}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


def _schedule_placeholder_rows(existing_rows: list[dict], weeks: list[str], projects: list[dict],
                               project_filter: Optional[str], person_scope: Optional[str],
                               name_map: dict) -> list[dict]:
    """Assignment rows schedule_grid wouldn't otherwise surface: a person is on
    a project's People property but has logged no allocation for it yet.
    Shaped exactly like schedule_grid's rows (same keys, all-zero cells) so
    the template renders them as ordinary, empty-input rows.

    project_filter: the selected ?project=, if any.
    person_scope: the person to scope to — the admin's ?person= pick, or the
    viewer's own id for non-admins. When both project_filter and person_scope
    are set, only their intersection (that one person/project pair) is added.
    """
    seen = {(r["person_id"], r["project_id"]) for r in existing_rows}
    empty_cells = {w: 0.0 for w in weeks}
    placeholders: list[dict] = []

    def add(pid, pname, prid, prname):
        key = (pid, prid)
        if key in seen:
            return
        seen.add(key)
        placeholders.append({
            "person_id": pid, "person_name": pname,
            "project_id": prid, "project_name": prname,
            "cells": dict(empty_cells),
        })

    if project_filter:
        proj = next((p for p in projects if p["id"] == project_filter), None)
        if proj:
            for mid in proj.get("member_ids", []):
                if person_scope and mid != person_scope:
                    continue  # a person filter is also active — only that intersection
                add(mid, name_map.get(mid, "(unnamed)"), proj["id"], proj["name"])

    if person_scope:
        pname = name_map.get(person_scope, "(unnamed)")
        for proj in projects:
            if project_filter and proj["id"] != project_filter:
                continue  # a project filter is also active — only that intersection
            if person_scope in proj.get("member_ids", []):
                add(person_scope, pname, proj["id"], proj["name"])

    return placeholders


@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, start: Optional[str] = None, by: str = "person",
                  person: Optional[str] = None, project: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    is_admin = auth.is_admin(user.get("email"))
    by = by if by in ("person", "project") else "person"
    mon = ops.monday_of(_parse_date(start))  # malformed ?start= falls back to today
    grid = ops.schedule_grid(mon, 6, None if is_admin else user.get("id"))
    rows = grid["rows"]
    if person:
        rows = [r for r in rows if r["person_id"] == person]
    if project:
        rows = [r for r in rows if r["project_id"] == project]

    people = ops.list_people() if is_admin else []
    projects = ops.list_projects(include_members=True)

    # Fill in assignments with no logged allocation: a project filter should
    # list every person on that project, a person filter (or a non-admin's
    # implicit self-scope) should list every project they're on. The
    # unfiltered admin view (person_scope and project_filter both unset)
    # is left untouched — allocation rows only, as today.
    person_scope = person if is_admin else user.get("id")
    if project or person_scope:
        name_map = {p["id"]: p["name"] for p in people} if is_admin else {person_scope: user.get("name")}
        placeholders = _schedule_placeholder_rows(rows, grid["weeks"], projects, project, person_scope, name_map)
        if placeholders:
            rows = sorted(rows + placeholders, key=lambda r: (r["person_name"].lower(), r["project_name"].lower()))

    # group rows by the chosen pivot, with per-group weekly totals
    groups: dict = {}
    for r in rows:
        gid = r["person_id"] if by == "person" else r["project_id"]
        gname = r["person_name"] if by == "person" else r["project_name"]
        g = groups.setdefault(gid, {"id": gid, "name": gname, "rows": [],
                                    "totals": {w: 0.0 for w in grid["weeks"]}})
        g["rows"].append(r)
        for w in grid["weeks"]:
            g["totals"][w] += r["cells"][w]
    target = float(os.environ.get("WEEK_TARGET_HOURS", "40"))
    return templates.TemplateResponse(request, "schedule.html", {
        "user": user, "weeks": grid["weeks"], "by": by,
        "groups": sorted(groups.values(), key=lambda g: g["name"].lower()),
        "focus_person": person or "", "focus_project": project or "",
        "is_admin": is_admin, "target": target,
        "people": people,
        "projects": projects,
        "prev_start": (mon - dt.timedelta(weeks=6)).isoformat(),
        "next_start": (mon + dt.timedelta(weeks=6)).isoformat(),
        "this_start": ops.monday_of().isoformat(),
        "start_iso": mon.isoformat(),
    })


class Alloc(BaseModel):
    person_id: str
    project_id: str
    week: str
    hours: float = Field(ge=0, le=168, allow_inf_nan=False)


@app.post("/api/allocation")
def api_allocation(request: Request, alloc: Alloc):
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user.get("email")):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    week = _parse_date(alloc.week)
    if not week:
        return JSONResponse({"ok": False, "error": "invalid week date"}, status_code=400)
    week = ops.monday_of(week).isoformat()  # normalize: allocations always live on Mondays
    try:
        return JSONResponse(ops.set_allocation(alloc.person_id, alloc.project_id, week, alloc.hours))
    except Exception:
        return JSONResponse({"ok": False, "error": "could not save allocation"}, status_code=400)


class Cell(BaseModel):
    project_id: str
    date: str
    hours: float = Field(ge=0, le=24, allow_inf_nan=False)
    person_id: Optional[str] = None  # ignored server-side; kept for client compat


@app.post("/api/cell")
def api_cell(request: Request, cell: Cell):
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    # Always write as the logged-in user — ignore any client-supplied person_id
    # so nobody can edit someone else's hours.
    person_id = user.get("id")
    if not person_id:
        return JSONResponse({"ok": False, "error": "no user identity"}, status_code=400)
    if not _parse_date(cell.date) or len(cell.date) != 10:
        return JSONResponse({"ok": False, "error": "invalid date"}, status_code=400)
    # membership is enforced on write, not just in the picker
    if cell.hours and cell.project_id not in _member_project_ids(person_id):
        return JSONResponse({"ok": False, "error": "not a member of that project"}, status_code=403)
    try:
        return JSONResponse(ops.set_cell(person_id, cell.project_id, cell.date, cell.hours))
    except Exception:
        return JSONResponse({"ok": False, "error": "could not save entry"}, status_code=400)
