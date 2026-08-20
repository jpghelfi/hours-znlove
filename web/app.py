"""FastAPI web app for the Notion hours tracker.

Notion is the source of truth; this app is a nicer entry form and an editable
Mon–Fri weekly grid on top of it, behind Notion OAuth login (allowlisted).

Run:  ./.venv/bin/uvicorn web.app:app --reload --port 8000
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from . import google_auth
from . import mailer
from . import notion_ops as ops
from . import report_gsheet
from . import report_xlsx

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


_asset_cache: dict[str, tuple[float, str]] = {}


def asset_v(name: str) -> str:
    """Short content hash of a static file, used as ?v= on its <link>/<script>.

    Starlette serves /static with an ETag but no Cache-Control, so browsers fall
    back to heuristic caching and can keep a stale stylesheet for days after a
    deploy — which is how new HTML ended up rendering against old CSS. The hash
    changes with the file, so a deploy always yields a fresh URL.

    Keyed on mtime rather than computed once at import: otherwise editing CSS
    under a running server keeps handing out the old (immutable-cached) URL and
    local changes never show up.
    """
    path = BASE / "static" / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "dev"
    hit = _asset_cache.get(name)
    if not hit or hit[0] != mtime:
        _asset_cache[name] = (mtime, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
    return _asset_cache[name][1]


templates.env.globals["asset_v"] = asset_v


@app.middleware("http")
async def _static_cache_headers(request: Request, call_next):
    """Hash-stamped assets are immutable; an unstamped URL must revalidate."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if request.query_params.get("v")
            else "no-cache"
        )
    return response


@app.on_event("startup")
def _startup() -> None:
    ops.ensure_person_property()
    ops.ensure_admin_property()
    ops.ensure_task_properties()
    ops.ensure_invoice_properties()


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


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
    """Return the user dict, or None if the caller should be redirected to login.

    Re-checks the People-db roster every request (cheap — access_ids is
    TTL-cached), so unticking someone's Active row actually ends their live
    session within the cache TTL, not just blocks their next login. Skipped in
    AUTH_DISABLED dev mode, where there's no real roster to check against."""
    user = current_user(request)
    if user and not auth.auth_disabled() and not auth.is_allowed(user):
        request.session.pop("user", None)
        return None
    return user


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
    return templates.TemplateResponse(request, "login.html", {"denied": request.query_params.get("denied"), "is_admin": False})


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
    if not auth.is_allowed(user):
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
        "is_admin": auth.is_admin(user),
        "projects": ops.list_projects(member_of=user.get("id")),
        "today": dt.date.today().isoformat(),
        "ticket_create": ops.ticket_create_enabled(),
        "ok": ok, "err": _ENTRY_ERRORS.get(err) if err else None,
    })


_ENTRY_ERRORS = {
    "date": "That date isn't valid — use the date picker.",
    "hours": "Hours must be between 0.25 and 24.",
    "project": "Pick one of your projects.",
    "save": "Couldn't save the entry — try again.",
    "task": "That doesn't look like a link to a Notion page.",
}


@app.post("/entry")
def submit_entry(
    request: Request,
    project_id: str = Form(...),
    date: str = Form(...),
    hours: float = Form(...),
    description: str = Form(""),
    task_url: str = Form(""),
    task_label: str = Form(""),
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
    # The ticket link is re-parsed here rather than trusted: the browser sends
    # it, so a junk or non-Notion URL must never reach the entry.
    task = ops.parse_task_url(task_url) if task_url else None
    if task_url and not task:
        return RedirectResponse(url="/?err=task", status_code=303)
    if task and (ops.resolve_task(task["id"]) or {}).get("ours"):
        # the picker already refuses these; this catches a hand-made POST
        return RedirectResponse(url="/?err=task", status_code=303)
    # the label the picker resolved, else the one in the link's own slug
    label = (task_label.strip() or task["label"] or "Notion ticket") if task else ""
    try:
        ops.create_entry(user.get("id"), project_id, date, hours, description,
                         task_url=task["url"] if task else "", task_label=label)
    except Exception:
        return RedirectResponse(url="/?err=save", status_code=303)
    return RedirectResponse(url="/?ok=1", status_code=303)


@app.get("/api/tasks/search")
def api_tasks_search(request: Request, q: str = "", mine: int = 0):
    """Ticket picker: tickets assigned to the caller, or a title search.

    Scoped by the ticket's assignee, matched against the caller's own Notion
    identity — the id they logged in with, or their email for boards that keep
    the assignee in an email column. An empty q lists just their tickets; a
    query searches every connected board, theirs sorted first.
    """
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login required"}, status_code=401)
    if not ops.task_sources():
        # No ticket board is connected to the integration yet — say so plainly
        # instead of returning an empty list that reads like "no tickets".
        return JSONResponse({"ok": True, "results": [], "connected": False})
    try:
        results = ops.search_tasks(q[:100], user.get("id"), user.get("email"),
                                   mine_only=bool(mine))
    except Exception:
        logging.exception("Ticket search failed for %s", user.get("email"))
        return JSONResponse({"ok": False, "error": "search failed"}, status_code=502)
    return JSONResponse({"ok": True, "results": results, "connected": True})


@app.get("/api/tasks/resolve")
def api_tasks_resolve(request: Request, url: str = ""):
    """A pasted link -> the ticket to attach.

    Parsing is offline and always works; the Notion read only *upgrades* the
    label to the ticket's real title, and its failure is not an error — it just
    means the page isn't shared with this app.
    """
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login required"}, status_code=401)
    task = ops.parse_task_url(url[:600])
    if not task:
        return JSONResponse({"ok": False, "error": "not a Notion page link"}, status_code=400)
    live = ops.resolve_task(task["id"])
    if live and live.get("ours"):
        return JSONResponse({"ok": False, "error": "that's an Hours Tracker page, not a ticket"},
                            status_code=400)
    return JSONResponse({"ok": True, "url": task["url"], "verified": bool(live),
                         "label": (live or {}).get("title") or task["label"] or "Notion ticket"})


@app.get("/api/tasks/new")
def api_tasks_new(request: Request, project_id: str = ""):
    """What the "new ticket" dialog needs before it opens.

    The project name is looked up from *our* Projects db by id rather than taken
    from the browser, so the option we preselect is the one that belongs to a
    project this person is actually on.
    """
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login required"}, status_code=401)
    if not ops.ticket_create_enabled():
        return JSONResponse({"ok": True, "enabled": False, "options": [], "selected": ""})
    name = ""
    if project_id:
        for p in ops.list_projects(member_of=user.get("id")):
            if p["id"] == project_id:
                name = p["name"]
                break
    try:
        info = ops.ticket_board_info(name)
    except Exception:
        logging.exception("Reading the ticket board's schema failed")
        return JSONResponse({"ok": False, "error": "board unavailable"}, status_code=502)
    return JSONResponse({"ok": True, "project": name, **info})


@app.post("/api/tasks/create")
def api_tasks_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    project_option: str = Form(""),
):
    """Create a ticket on the configured board and hand it back to the picker.

    The ticket is created *now*, not when the hours are saved — it is a real
    page on a real board, so the dialog says as much.
    """
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login required"}, status_code=401)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    if not ops.ticket_create_enabled():
        return JSONResponse({"ok": False, "error": "creating tickets isn't set up"},
                            status_code=403)
    if not title.strip():
        # the dialog marks it required; this catches a hand-made POST
        return JSONResponse({"ok": False, "error": "A ticket needs a title."}, status_code=400)
    try:
        ticket = ops.create_ticket(title[:200], description[:8000],
                                   project_option, user.get("id"))
    except Exception as exc:
        logging.exception("Creating a ticket failed for %s", user.get("email"))
        return JSONResponse({"ok": False, "error": _ticket_error(exc)}, status_code=502)
    return JSONResponse({"ok": True, **ticket})


def _ticket_error(exc: Exception) -> str:
    """Notion's refusal in a sentence someone can act on — the same courtesy
    mailer.explain() does for Google's errors."""
    text = str(exc)
    if "restricted" in text.lower() or "permission" in text.lower() or "unauthorized" in text.lower():
        return ("Hours Tracker can't create pages on that board yet — in Notion, open it "
                "→ ••• → Connections → add Hours Tracker.")
    if "validation" in text.lower():
        return "Notion refused that ticket: " + text[:200]
    return "Couldn't create the ticket — try again, or add it in Notion."


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
        "is_admin": auth.is_admin(user),
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


def _report_data(user, scope, range_key, date_from, date_to, people=None):
    f, t, rk = _range_bounds(range_key, date_from, date_to)
    is_admin = auth.is_admin(user)
    # Picking specific people is a team-wide read narrowed down, so it implies
    # team scope (admins only, like everything else on this page).
    selected = [p for p in (people or []) if p] if is_admin else []
    team = (scope == "team" or bool(selected)) and is_admin
    entries = ops.entries_between(f, t, None if team else user.get("id"))
    if selected:
        sel = set(selected)
        entries = [e for e in entries if e["person_id"] in sel]
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
    if selected:
        planned = [p for p in planned if p["person_id"] in sel]

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
        "selected": selected,
        "person_projects": _person_projects(entries),
        "matrix": _person_project_matrix(entries),
    }


def _person_projects(entries):
    """Per person, their projects — the 'by person, then by project' view.
    Sorted by hours descending at both levels."""
    per = {}
    for e in entries:
        p = per.setdefault(e["person"], {"name": e["person"], "hours": 0, "projects": {}})
        p["hours"] += e["hours"]
        p["projects"][e["project"]] = p["projects"].get(e["project"], 0) + e["hours"]
    out = []
    for p in sorted(per.values(), key=lambda p: (-p["hours"], p["name"].lower())):
        mx = max(p["projects"].values(), default=0)
        out.append({
            "name": p["name"], "hours": round(p["hours"], 2),
            "projects": [{"name": n, "hours": round(v, 2), "pct": round(v / mx * 100) if mx else 0}
                         for n, v in sorted(p["projects"].items(), key=lambda kv: (-kv[1], kv[0].lower()))],
        })
    return out


def _person_project_matrix(entries):
    """Projects (rows) × people (columns) with row/column totals — the same
    numbers as person_projects, pivoted for scanning across people."""
    cells = {}
    ptot, jtot = {}, {}
    for e in entries:
        cells[(e["project"], e["person"])] = cells.get((e["project"], e["person"]), 0) + e["hours"]
        ptot[e["person"]] = ptot.get(e["person"], 0) + e["hours"]
        jtot[e["project"]] = jtot.get(e["project"], 0) + e["hours"]
    people = [n for n, _ in sorted(ptot.items(), key=lambda kv: (-kv[1], kv[0].lower()))]
    projects = [n for n, _ in sorted(jtot.items(), key=lambda kv: (-kv[1], kv[0].lower()))]
    return {
        "people": people,
        "rows": [{"project": j,
                  "cells": [round(cells.get((j, n), 0), 2) for n in people],
                  "total": round(jtot[j], 2)} for j in projects],
        "totals": [round(ptot[n], 2) for n in people],
        "grand": round(sum(jtot.values()), 2),
    }


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, scope: str = "me", range: Optional[str] = None,
                 date_from: Optional[str] = None, date_to: Optional[str] = None,
                 person: list[str] = Query(default=[])):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    data = _report_data(user, scope, range, date_from, date_to, person)
    return templates.TemplateResponse(request, "reports.html", {
        "user": user, "r": data, "scope": "team" if data["team"] else "me",
        "is_admin": True, "people": ops.list_people(),
    })


@app.get("/reports.csv")
def reports_csv(request: Request, scope: str = "me", range: Optional[str] = None,
                date_from: Optional[str] = None, date_to: Optional[str] = None,
                person: list[str] = Query(default=[])):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    data = _report_data(user, scope, range, date_from, date_to, person)
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "person", "project", "hours", "description", "task", "task_url"])
    for e in sorted(data["entries"], key=lambda e: (e["date"], e["person"])):
        w.writerow([e["date"], e["person"], e["project"], e["hours"], e["description"],
                    e.get("task", ""), e.get("task_url", "")])
    from fastapi.responses import Response
    fname = f"hours_{data['from']}_{data['to']}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ---- per-project hours -------------------------------------------------

_PERIODS = ("daily", "weekly", "monthly")


def _add_months(d: dt.date, n: int) -> dt.date:
    """First of the month n months from d's month (n may be negative)."""
    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    return dt.date(y, m + 1, 1)


def _project_anchor(period: str, start: Optional[str]) -> Optional[dt.date]:
    """Parse ?start= for this page. The monthly picker is an <input type=month>,
    which posts YYYY-MM — widen it to the 1st so the normal date parse works."""
    if start and period == "monthly" and len(start) == 7:
        start = start + "-01"
    return _parse_date(start)


def _period_range(period: str, anchor: Optional[dt.date]) -> dict:
    """The one period being viewed: its bounds, its label, and how to step off it.

    Deliberately a single period, not a window of several: you pick a month (or
    week, or day) and read one table of names and hours for it, the way Harvest
    does. `picker`/`value` drive the native date/month input in the toolbar.
    """
    today = dt.date.today()
    if period == "daily":
        day = anchor or today
        return {
            "from": day.isoformat(), "to": day.isoformat(),
            "label": day.strftime("%A, %d %B %Y"),
            "prev": (day - dt.timedelta(days=1)).isoformat(),
            "next": (day + dt.timedelta(days=1)).isoformat(),
            "now": today.isoformat(), "now_label": "Today",
            "picker": "date", "value": day.isoformat(),
        }
    if period == "monthly":
        first = (anchor or today).replace(day=1)
        last = _add_months(first, 1) - dt.timedelta(days=1)
        return {
            "from": first.isoformat(), "to": last.isoformat(),
            "label": first.strftime("%B %Y"),
            "prev": _add_months(first, -1).isoformat(),
            "next": _add_months(first, 1).isoformat(),
            "now": today.replace(day=1).isoformat(), "now_label": "This month",
            "picker": "month", "value": first.strftime("%Y-%m"),
        }
    # weekly: the Mon–Sun week the anchor falls in (Sunday included so weekend
    # entries from the CLI/backfill can't silently vanish)
    mon = ops.monday_of(anchor or today)
    sun = mon + dt.timedelta(days=6)
    return {
        "from": mon.isoformat(), "to": sun.isoformat(),
        "label": f"Week {mon.strftime('%V')} · {mon.strftime('%d %b')} – {sun.strftime('%d %b %Y')}",
        "prev": (mon - dt.timedelta(days=7)).isoformat(),
        "next": (mon + dt.timedelta(days=7)).isoformat(),
        "now": ops.monday_of(today).isoformat(), "now_label": "This week",
        "picker": "date", "value": mon.isoformat(),
    }


def _project_hours(project_id: str, rng: dict, member_ids: list, name_map: dict) -> dict:
    """One row per person: their hours on this project in the chosen period.

    People assigned to the project but with nothing logged still get a row, so
    the table answers "who is on this and what have they done" rather than only
    "who logged something".
    """
    entries = ops.project_entries(project_id, rng["from"], rng["to"])
    rows: dict = {}

    def row_for(pid, name):
        return rows.setdefault(pid or "(unassigned)", {
            "person_id": pid, "person_name": name,
            "hours": 0.0, "entries": 0, "days": set(), "log": [],
        })

    for e in entries:
        r = row_for(e["person_id"], e["person"])
        r["hours"] += e["hours"]
        r["entries"] += 1
        r["days"].add(e["date"])
        # the person's own entries, so their row can expand into what they did
        r["log"].append(e)
    for mid in member_ids:
        row_for(mid, name_map.get(mid, "(unnamed)"))

    ordered = sorted(rows.values(), key=lambda r: (-r["hours"], r["person_name"].lower()))
    total = round(sum(r["hours"] for r in ordered), 2)
    top = max([r["hours"] for r in ordered], default=0)
    for r in ordered:
        r["days"] = len(r["days"])
        # bar length is relative to the busiest person, share is of the project
        r["pct"] = round(r["hours"] / top * 100) if top else 0
        r["share"] = round(r["hours"] / total * 100) if total else 0
        r["hours"] = round(r["hours"], 2)
        r["log"].sort(key=lambda e: (e["date"], e["person"]), reverse=True)
    return {
        "rows": ordered, "total": total,
        "entries": sorted(entries, key=lambda e: (e["date"], e["person"]), reverse=True),
        "people_count": sum(1 for r in ordered if r["hours"]),
    }


def _all_projects_hours(rng: dict, projects: list, name_map: dict,
                        keep_ids: Optional[set] = None) -> dict:
    """Every project in the period, each with its people nested inside it.

    One Notion read for the whole period (entries_between), grouped by project
    id — not one query per project, which would be dozens of round trips.
    Projects with nothing logged are still listed (at the bottom, zeroed), and
    so are projects that only appear in the entries: an entry logged against an
    archived/inactive project must not silently drop out of the totals.

    `keep_ids` narrows the rollup to a picked set of projects: the filtering is
    done here, in memory, over that same single read (the Notion query stays
    unfiltered), so picking projects costs no extra round trips.
    """
    entries = ops.entries_between(rng["from"], rng["to"])
    if keep_ids is not None:
        entries = [e for e in entries if e["project_id"] in keep_ids]
        projects = [p for p in projects if p["id"] in keep_ids]
    groups: dict = {}

    def group_for(prid, name):
        return groups.setdefault(prid, {
            "project_id": prid, "project_name": name,
            "hours": 0.0, "entries": 0, "days": set(), "people": {},
        })

    for e in entries:
        g = group_for(e["project_id"], e["project"])
        g["hours"] += e["hours"]
        g["entries"] += 1
        g["days"].add(e["date"])
        key = e["person_id"] or "(unassigned)"
        p = g["people"].setdefault(key, {
            "person_name": e["person"], "hours": 0.0, "entries": 0, "days": set(),
            "log": [],
        })
        p["hours"] += e["hours"]
        p["entries"] += 1
        p["days"].add(e["date"])
        # their own entries, so a person row expands the same way here as it
        # does in the one-project view
        p["log"].append(e)

    for proj in projects:  # every active project shows up, logged against or not
        g = group_for(proj["id"], proj["name"])
        for mid in proj.get("member_ids", []):
            g["people"].setdefault(mid, {
                "person_name": name_map.get(mid, "(unnamed)"),
                "hours": 0.0, "entries": 0, "days": set(), "log": [],
            })

    total = round(sum(g["hours"] for g in groups.values()), 2)
    top = max([g["hours"] for g in groups.values()], default=0)
    ordered = sorted(groups.values(), key=lambda g: (-g["hours"], g["project_name"].lower()))
    for g in ordered:
        g["days"] = len(g["days"])
        g["pct"] = round(g["hours"] / top * 100) if top else 0
        g["share"] = round(g["hours"] / total * 100) if total else 0
        ptop = max([p["hours"] for p in g["people"].values()], default=0)
        for key, p in g["people"].items():
            p["key"] = key  # what the person's entry rows point back at
        rows = sorted(g["people"].values(), key=lambda p: (-p["hours"], p["person_name"].lower()))
        for p in rows:
            p["days"] = len(p["days"])
            p["log"].sort(key=lambda e: (e["date"], e["person"]), reverse=True)
            # person bars are scaled within their project, so an expanded group
            # shows that project's split rather than a sliver of the biggest one
            p["pct"] = round(p["hours"] / ptop * 100) if ptop else 0
            p["share"] = round(p["hours"] / g["hours"] * 100) if g["hours"] else 0
            p["hours"] = round(p["hours"], 2)
        g["rows"] = rows
        g["hours"] = round(g["hours"], 2)
    return {
        "groups": ordered, "total": total, "entries": entries,
        "projects_count": sum(1 for g in ordered if g["hours"]),
        "people_count": len({e["person_id"] for e in entries}),
    }


def _project_picks(projects: list, picked: list) -> tuple:
    """Resolve repeated ?project= into (selected ids, the one project or None).

    Ids that no longer exist (and the "all" sentinel, which older links and the
    "All projects" checkbox both send) are dropped, so a stale bookmark degrades
    to the rollup rather than to an empty page. Exactly one pick keeps the
    per-person drill-in; none or several read as the rollup.
    """
    known = {p["id"]: p for p in projects}
    sel_ids, seen = [], set()
    for pid in picked or []:
        if pid in known and pid not in seen:
            seen.add(pid)
            sel_ids.append(pid)
    sel = known[sel_ids[0]] if len(sel_ids) == 1 else None
    return sel_ids, sel


@app.get("/project", response_class=HTMLResponse)
def project_page(request: Request, project: list[str] = Query(default=[]),
                 period: str = "monthly", start: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    period = period if period in _PERIODS else "monthly"
    projects = ops.list_projects(include_members=True)
    # ?project= repeats: no pick (or "all") is the every-project rollup, one
    # pick drills into that project, several roll up just those projects
    sel_ids, sel = _project_picks(projects, project)
    is_all = sel is None
    rng = _period_range(period, _project_anchor(period, start))
    people = ops.list_people()
    name_map = {p["id"]: p["name"] for p in people}
    data = (_all_projects_hours(rng, projects, name_map, set(sel_ids) or None) if is_all
            else _project_hours(sel["id"], rng, sel.get("member_ids", []), name_map))
    return templates.TemplateResponse(request, "project.html", {
        "user": user, "is_admin": True,
        "projects": projects, "sel": sel, "sel_ids": sel_ids, "is_all": is_all,
        "period": period, "rng": rng,
        "can_invoice": bool(ops.invoices_enabled() and sel and period == "monthly"),
        "d": data, "start_iso": rng["from"],
    })


def _period_entries(sel: Optional[dict], sel_ids: list, rng: dict) -> list[dict]:
    """The entries behind the current period + project picks, shared by every
    export. One project reads through the Notion-side relation filter; a pick of
    several (or none) narrows the single period read in memory."""
    if sel:
        return [dict(e, project=sel["name"], project_id=sel["id"])
                for e in ops.project_entries(sel["id"], rng["from"], rng["to"])]
    entries = ops.entries_between(rng["from"], rng["to"])
    if sel_ids:
        keep = set(sel_ids)
        entries = [e for e in entries if e["project_id"] in keep]
    return entries


def _export_label(sel: Optional[dict], sel_ids: list) -> str:
    return sel["name"] if sel else (f"{len(sel_ids)} projects" if sel_ids else "All projects")


def _export_slug(label: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in label).strip("-").lower() or "project"


@app.get("/project.csv")
def project_csv(request: Request, project: list[str] = Query(default=[]),
                period: str = "monthly", start: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    period = period if period in _PERIODS else "monthly"
    projects = ops.list_projects(include_members=True)
    sel_ids, sel = _project_picks(projects, project)
    rng = _period_range(period, _project_anchor(period, start))
    entries = _period_entries(sel, sel_ids, rng)
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "person", "project", "hours", "description", "task", "task_url"])
    for e in sorted(entries, key=lambda e: (e["date"], e["project"], e["person"])):
        w.writerow([e["date"], e["person"], e["project"], e["hours"], e["description"],
                    e.get("task", ""), e.get("task_url", "")])
    from fastapi.responses import Response
    fname = f"{_export_slug(_export_label(sel, sel_ids))}_{rng['from']}_{rng['to']}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ---- the shareable export: grouped workbook, adjustable, emailable ------

_XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAX_EXPORT_ROWS = 5000


def _xlsx_response(book: bytes, label: str, rng: dict):
    from fastapi.responses import Response
    fname = f"hours_{_export_slug(label)}_{rng['from']}_{rng['to']}.xlsx"
    return Response(book, media_type=_XLSX_TYPE,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _export_rows(entries: list[dict], name_map: dict) -> list[dict]:
    """Entries trimmed to what the workbook shows, with names resolved against
    the roster (people properties come back nameless — see notion_ops)."""
    return [{
        "id": e.get("id"),
        "project_id": e.get("project_id"),
        "project": e.get("project") or "(none)",
        "person_id": e.get("person_id"),
        "person": name_map.get(e.get("person_id")) or e.get("person") or "(unassigned)",
        "date": e["date"],
        "hours": e["hours"],
        "description": e.get("description") or "",
        "task": e.get("task") or "",
        "task_url": e.get("task_url") or "",
    } for e in entries]


def _rows_from_payload(rows: list) -> list[dict]:
    """Rows as the export screen posts them back — hours and comments possibly
    edited there. Deliberately *not* written anywhere: they exist for the length
    of this request, so a client-facing tweak never rewrites a logged entry."""
    if len(rows) > _MAX_EXPORT_ROWS:
        raise ValueError("too many rows")
    out = []
    for r in rows:
        try:
            hours = float(r.get("hours") or 0)
        except (TypeError, ValueError):
            raise ValueError("invalid hours")
        if not 0 <= hours <= 24:
            raise ValueError("hours out of range")
        if not hours:  # a row zeroed on the export screen is left out of the file
            continue
        date = str(r.get("date") or "")[:10]
        if not _parse_date(date):
            raise ValueError("invalid date")
        out.append({
            "project_id": str(r.get("project_id") or "")[:64] or None,
            "project": str(r.get("project") or "(none)")[:200],
            "person": str(r.get("person") or "(unassigned)")[:200],
            "date": date,
            "hours": round(hours, 2),
            "description": str(r.get("description") or "")[:2000],
            # the ticket rides along read-only, but it still arrives from the
            # browser, so the URL is re-parsed rather than trusted into a file
            "task": str(r.get("task") or "")[:200],
            "task_url": (ops.parse_task_url(str(r.get("task_url") or "")[:600]) or {}).get("url", ""),
        })
    return out


@app.get("/project.xlsx")
def project_xlsx(request: Request, project: list[str] = Query(default=[]),
                 period: str = "monthly", start: Optional[str] = None):
    """The report as a workbook — a sheet per project, people then their log."""
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    period = period if period in _PERIODS else "monthly"
    projects = ops.list_projects(include_members=True)
    sel_ids, sel = _project_picks(projects, project)
    rng = _period_range(period, _project_anchor(period, start))
    name_map = {p["id"]: p["name"] for p in ops.list_people()}
    rows = _export_rows(_period_entries(sel, sel_ids, rng), name_map)
    label = _export_label(sel, sel_ids)
    return _xlsx_response(report_xlsx.build(rows, rng["label"], label), label, rng)


@app.get("/project/export", response_class=HTMLResponse)
def project_export_page(request: Request, project: list[str] = Query(default=[]),
                        period: str = "monthly", start: Optional[str] = None,
                        sent: Optional[str] = None, err: Optional[str] = None):
    """Adjust the numbers for the copy that leaves the building, then download
    or email it. Edits here never touch the logged hours — that is the whole
    point of the screen: the inline editor on /project corrects what was logged,
    this one prepares what a client sees."""
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    period = period if period in _PERIODS else "monthly"
    projects = ops.list_projects(include_members=True)
    sel_ids, sel = _project_picks(projects, project)
    rng = _period_range(period, _project_anchor(period, start))
    name_map = {p["id"]: p["name"] for p in ops.list_people()}
    rows = sorted(_export_rows(_period_entries(sel, sel_ids, rng), name_map),
                  key=lambda r: (r["project"].lower(), r["date"], r["person"].lower()))
    groups = []
    for r in rows:  # rows arrive project-sorted, so a running group is enough
        if not groups or groups[-1]["name"] != r["project"]:
            groups.append({"name": r["project"], "rows": []})
        groups[-1]["rows"].append(r)
    for g in groups:
        g["hours"] = round(sum(r["hours"] for r in g["rows"]), 2)
    # An invoice is one project's month, so the screen only offers it when
    # that's what you're looking at; the POST re-checks both (see _invoicable).
    can_invoice = bool(ops.invoices_enabled() and sel and period == "monthly")
    existing = ops.find_invoice(sel["id"], rng["from"]) if can_invoice else None
    return templates.TemplateResponse(request, "project_export.html", {
        "user": user, "is_admin": True,
        "projects": projects, "sel": sel, "sel_ids": sel_ids,
        "period": period, "rng": rng, "start_iso": rng["from"],
        "label": _export_label(sel, sel_ids),
        "can_invoice": can_invoice, "existing_invoice": existing,
        "today_iso": dt.date.today().isoformat(),
        "groups": groups, "total": round(sum(r["hours"] for r in rows), 2),
        "recipients": ", ".join(mailer.default_recipients()),
        "sender": mailer.sender(), "mail_ready": mailer.configured(),
        "missing_vars": mailer.missing_vars(), "via": mailer.transport(),
        "gsheet_ready": google_auth.configured(),
        "sent": sent, "err": err,
    })


class ExportRequest(BaseModel):
    rows: list[dict] = Field(default_factory=list)
    label: str = "Hours"
    period_label: str = ""
    date_from: str = ""
    date_to: str = ""


class InvoiceRequest(ExportRequest):
    project_id: str = ""
    month: str = ""
    issued: str = ""
    note: str = ""


# ---- invoices ----------------------------------------------------------

@app.post("/api/invoice")
def api_save_invoice(request: Request, req: InvoiceRequest):
    """Record what we billed for one project, one month.

    The billed hours come from the screen — that's the human decision. The
    *tracked* hours are re-read from Notion rather than taken from the payload:
    a row zeroed on the export screen drops out of the file, so trusting the
    browser's total would quietly shrink what we say was tracked.
    """
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    if not ops.invoices_enabled():
        return JSONResponse({"ok": False, "error": "the Invoices database isn't set up yet"},
                            status_code=503)

    month = _parse_date(req.month)
    if not month or month.day != 1:
        return JSONResponse({"ok": False, "error": "an invoice covers a calendar month"},
                            status_code=400)
    projects = {p["id"]: p for p in ops.list_projects(include_members=True)}
    project = projects.get(req.project_id)
    if not project:
        return JSONResponse({"ok": False, "error": "pick one project to invoice"},
                            status_code=400)
    issued = _parse_date(req.issued) or dt.date.today()
    try:
        rows = _rows_from_payload(req.rows)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    rng = _period_range("monthly", month)
    logged = {e["id"]: e["hours"] for e in _period_entries(project, [project["id"]], rng)
              if e.get("id")}
    tracked = sum(logged.values())
    billed = sum(r["hours"] for r in rows)
    try:
        saved = ops.save_invoice(project["id"], month.isoformat(), tracked, billed,
                                 issued.isoformat(), req.note[:500], user.get("id"),
                                 adjustments=_invoice_adjustments(req.rows, logged))
    except Exception:
        logging.exception("Saving the invoice for %s %s failed", project["name"], req.month)
        return JSONResponse({"ok": False, "error": "could not save that invoice"},
                            status_code=502)
    return JSONResponse({"ok": True, "invoice": saved})


def _invoice_adjustments(raw_rows: list, logged: dict) -> dict:
    """{entry id: billed hours} for the rows billed at something other than
    what was logged — including the ones billed at nothing.

    Read from the *raw* payload rather than `_rows_from_payload`, which drops
    zeroed rows before they can be seen: "billed nothing" is precisely the
    adjustment most worth recording, and it would otherwise look like an entry
    that was never on screen.
    """
    out = {}
    for r in raw_rows:
        entry_id = str(r.get("id") or "")
        if entry_id not in logged:
            continue
        try:
            billed = round(float(r.get("hours") or 0), 2)
        except (TypeError, ValueError):
            continue
        if abs(billed - round(logged[entry_id], 2)) >= 0.01:
            out[entry_id] = billed
    return out


def _invoiced_rows(invoice: dict, entries: list[dict]) -> list[dict]:
    """The entries as this invoice billed them: what was logged, plus the
    adjustments recorded at save time.

    Entries are re-read live rather than copied onto the invoice, so this shows
    today's comments and any entry logged after the fact — which is the same
    reason the list can flag an invoice whose month has moved.
    """
    adj = {ops._bare(k): v for k, v in (invoice.get("adjustments") or {}).items()}
    rows = []
    for e in sorted(entries, key=lambda e: (e["date"], e["person"].lower())):
        billed = adj.get(ops._bare(e.get("id") or ""), e["hours"])
        rows.append(dict(e, tracked=e["hours"], billed=billed,
                         changed=abs(billed - e["hours"]) >= 0.01))
    return rows


def _invoiced_days(rows: list[dict]) -> list[dict]:
    """Those rows grouped into days, each with its own tracked/billed totals."""
    days: dict = {}
    for r in rows:
        day = days.setdefault(r["date"], {"date": r["date"], "rows": [],
                                          "tracked": 0.0, "billed": 0.0})
        day["rows"].append(r)
        day["tracked"] += r["tracked"]
        day["billed"] += r["billed"]
    out = []
    for day in sorted(days.values(), key=lambda d: d["date"]):
        day["tracked"] = round(day["tracked"], 2)
        day["billed"] = round(day["billed"], 2)
        parsed = _parse_date(day["date"])
        day["label"] = parsed.strftime("%a %d %b") if parsed else day["date"]
        out.append(day)
    return out


_STALE_MONTHS = 4


def _mark_stale(rows: list[dict]) -> None:
    """Flag invoices whose month has been logged against since they were saved.

    Someone always logs late, and without this nobody finds out. Checked only
    for the last few months, and with a *single* read of that window rather
    than one query per invoice — an invoice list two years long would otherwise
    cost two years of round trips to draw.
    """
    if not rows:
        return
    cutoff = _add_months(dt.date.today().replace(day=1), -(_STALE_MONTHS - 1))
    recent = [r for r in rows if (_parse_date(r["month"]) or dt.date.min) >= cutoff]
    if not recent:
        return
    to = _add_months(cutoff, _STALE_MONTHS) - dt.timedelta(days=1)
    logged: dict = {}
    for e in ops.entries_between(cutoff.isoformat(), to.isoformat()):
        day = _parse_date(e["date"])
        if day:
            key = (e.get("project_id"), day.replace(day=1).isoformat())
            logged[key] = logged.get(key, 0) + e["hours"]
    for r in recent:
        now = round(logged.get((r["project_id"], r["month"]), 0), 2)
        r["hours_now"] = now
        r["stale"] = abs(now - round(r["hours_tracked"], 2)) >= 0.01


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, project: list[str] = Query(default=[])):
    """What we've billed, newest month first (admins).

    Shows tracked next to billed so the gap is visible, and flags a month whose
    logged hours have moved since it was invoiced — which happens whenever
    somebody logs late.
    """
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    projects = ops.list_projects(include_members=True, active_only=False)
    sel_ids, sel = _project_picks(projects, project)
    rows = ops.list_invoices(sel["id"] if sel else None)
    if sel_ids and not sel:          # several picked: filter the one read in memory
        keep = set(sel_ids)
        rows = [r for r in rows if r["project_id"] in keep]
    _mark_stale(rows)
    for r in rows:
        month = _parse_date(r["month"])
        r["month_label"] = month.strftime("%B %Y") if month else r["month"]
    return templates.TemplateResponse(request, "invoices.html", {
        "user": user, "is_admin": True,
        "projects": projects, "sel": sel, "sel_ids": sel_ids,
        "rows": rows,
        "enabled": ops.invoices_enabled(),
        "total_billed": round(sum(r["hours_billed"] for r in rows), 2),
        "total_tracked": round(sum(r["hours_tracked"] for r in rows), 2),
    })


def _invoice_export_rows(invoice: dict) -> tuple[list[dict], dict]:
    """The invoice's lines shaped for a file: billed hours in the `hours` slot,
    lines billed at nothing left out.

    The file carries what was **billed** — the tracked column is the internal
    working number, and putting both in front of a client only invites an
    argument about the difference.
    """
    month = _parse_date(invoice["month"])
    if not month or not invoice["project_id"]:
        return [], {"from": invoice["month"], "to": invoice["month"], "label": invoice["month"]}
    rng = _period_range("monthly", month)
    project = {"id": invoice["project_id"], "name": invoice["project"]}
    rows = []
    for r in _invoiced_rows(invoice, _period_entries(project, [project["id"]], rng)):
        if not r["billed"]:
            continue
        rows.append({**r, "hours": round(r["billed"], 2), "project": invoice["project"],
                     "project_id": invoice["project_id"]})
    return rows, rng


@app.get("/invoices/{invoice_id}.xlsx")
def invoice_xlsx(request: Request, invoice_id: str):
    """The invoice as a workbook — the billed hours, ready to send on."""
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    invoice = ops.get_invoice(invoice_id)
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=303)
    rows, rng = _invoice_export_rows(invoice)
    month = _parse_date(invoice["month"])
    label = invoice["project"]
    period_label = month.strftime("%B %Y") if month else invoice["month"]
    return _xlsx_response(report_xlsx.build(rows, period_label, label), label, rng)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: str):
    """One invoice, day by day, as it was billed.

    The hours shown are the *invoiced* ones — what was logged, with the
    adjustments made on the export screen applied back over them. Rows billed
    at less (or nothing) are marked, so the difference on the list has an
    explanation you can read.
    """
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    invoice = ops.get_invoice(invoice_id)
    if not invoice:
        return RedirectResponse(url="/invoices", status_code=303)

    month = _parse_date(invoice["month"])
    rng = _period_range("monthly", month) if month else None
    rows, days = [], []
    if rng and invoice["project_id"]:
        project = {"id": invoice["project_id"], "name": invoice["project"]}
        rows = _invoiced_rows(invoice, _period_entries(project, [project["id"]], rng))
        days = _invoiced_days(rows)
    now_tracked = round(sum(r["tracked"] for r in rows), 2)
    # what the clipboard button pastes into a sheet: the billed lines only,
    # the same set the workbook holds
    sheet_rows = [{"project": invoice["project"], "date": r["date"], "person": r["person"],
                   "hours": round(r["billed"], 2), "description": r["description"],
                   "task_url": r.get("task_url") or ""}
                  for r in rows if r["billed"]]
    return templates.TemplateResponse(request, "invoice_detail.html", {
        "sheet_rows": sheet_rows,
        "user": user, "is_admin": True,
        "invoice": invoice, "days": days, "rng": rng,
        "month_label": month.strftime("%B %Y") if month else invoice["month"],
        "now_tracked": now_tracked,
        "now_billed": round(sum(r["billed"] for r in rows), 2),
        # the month has been logged against since this was saved
        "stale": abs(now_tracked - round(invoice["hours_tracked"], 2)) >= 0.01,
        "adjusted": sum(1 for r in rows if r["changed"]),
        # saved before the per-line adjustments were recorded: the total says
        # one thing and the days below would add up to another, so say why
        # rather than showing a contradiction
        "legacy": (not invoice.get("adjustments")
                   and abs(round(invoice["hours_billed"], 2)
                           - round(invoice["hours_tracked"], 2)) >= 0.01),
    })


class EmailRequest(ExportRequest):
    to: str = ""
    subject: str = ""
    message: str = ""


@app.post("/project/export.xlsx")
def project_export_xlsx(request: Request, payload: str = Form(...)):
    """Download the workbook built from the (possibly adjusted) rows on screen."""
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    try:
        req = ExportRequest.model_validate_json(payload)
        rows = _rows_from_payload(req.rows)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    rng = {"from": req.date_from or "", "to": req.date_to or ""}
    return _xlsx_response(report_xlsx.build(rows, req.period_label, req.label),
                          req.label, rng)


@app.post("/project/export.gsheet")
def project_export_gsheet(request: Request, req: ExportRequest):
    """Create the report as a named Google Sheet and return its URL."""
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    if not google_auth.configured():
        return JSONResponse({"ok": False, "error": "Google isn't connected yet — add "
                             + ", ".join(google_auth.missing_vars())
                             + " to the environment"}, status_code=503)
    try:
        rows = _rows_from_payload(req.rows)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if not rows:
        return JSONResponse({"ok": False, "error": "nothing to put in a sheet — every row is 0"},
                            status_code=400)
    try:
        res = report_gsheet.create(rows, req.period_label, req.label)
    except google_auth.GoogleError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except Exception:
        logging.exception("Creating the sheet for %s failed", req.label)
        return JSONResponse({"ok": False, "error": "could not create that sheet"},
                            status_code=502)
    return JSONResponse({"ok": True, **res})


@app.post("/api/report/email")
def api_report_email(request: Request, req: EmailRequest):
    """Email the workbook from the sender's own mailbox (SMTP_USER)."""
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    if not mailer.enabled():
        # the switch, not just the credentials: the endpoint stays shut even
        # when Google is connected for the Sheets export
        return JSONResponse({"ok": False, "error": "emailing reports is turned off"},
                            status_code=403)
    if not mailer.configured():
        return JSONResponse({"ok": False, "error": "email isn't set up yet — add "
                             + ", ".join(mailer.missing_vars())
                             + " to the environment"}, status_code=503)
    try:
        to = mailer.clean_recipients(req.to or mailer.default_recipients())
        rows = _rows_from_payload(req.rows)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if not rows:
        return JSONResponse({"ok": False, "error": "nothing to send — every row is 0"},
                            status_code=400)
    book = report_xlsx.build(rows, req.period_label, req.label)
    fname = f"hours_{_export_slug(req.label)}_{req.date_from}_{req.date_to}.xlsx"
    subject = req.subject.strip() or f"Hours — {req.label} — {req.period_label}"
    body = req.message.strip() or (
        f"Hi,\n\nAttached are the hours for {req.label} — {req.period_label}: "
        f"{round(sum(r['hours'] for r in rows), 2):g} h across {len(rows)} entries.\n\n"
        f"— {user.get('name') or user.get('email') or 'Hours Tracker'}\n")
    try:
        res = mailer.send_report(to, subject, body, book, fname)
    except mailer.NotConfigured as e:
        return JSONResponse({"ok": False, "error": f"missing {e}"}, status_code=503)
    except Exception as e:
        logging.exception("Sending the report to %s failed", to)
        # the screen is admin-only, so hand back what the mail server actually
        # said — "rejected" alone can't tell a bad password from a bad address
        return JSONResponse({"ok": False, "error": mailer.explain(e)}, status_code=502)
    return JSONResponse(res)


_PALETTE_SIZE = 8  # .pill.s0 … .pill.s7 (--s0 … --s7) in style.css


def _swatch(key: str) -> int:
    """Stable palette slot for a project id, so a project keeps its pill color
    across days, views and page loads (and matches between the two groupings)."""
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) % _PALETTE_SIZE


def _current_monday() -> dt.date:
    """The week the planner opens on. On a weekend there's nothing left to plan
    in the week just gone, so roll forward to the upcoming Monday."""
    today = dt.date.today()
    if today.weekday() >= 5:
        return today + dt.timedelta(days=7 - today.weekday())
    return ops.monday_of(today)


def _day_target() -> float:
    """Hours a person is expected to be booked for on a weekday.

    DAY_TARGET_HOURS is the knob (default 8). WEEK_TARGET_HOURS stays supported
    as an override for the weeks rollup so existing deploys keep their number.
    """
    return float(os.environ.get("DAY_TARGET_HOURS", "8"))


def _schedule_rows(allocs: list[dict], cols: list[str], by: str, bucket,
                   people: list[dict], projects: list[dict],
                   focus_people: set, focus_project: Optional[str]) -> list[dict]:
    """Planner rows: one per person (by="person") or per project, each holding
    a stack of pills per column. Returns (rows, hidden) — see the pruning note.

    Every roster person/project gets a row even with nothing booked — an empty
    row is what you click to make the first assignment, so the old
    "add a person/project pair first, then type hours" step disappears.
    bucket maps an allocation's date to its column (identity for the day
    planner, the week's Monday for the rollup).

    The exception is a view narrowed to *one project*: "the Fotosprint week"
    means the people on Fotosprint, not the whole company with four rows filled
    in, so empty rows are pruned there (and symmetrically for projects when the
    view is narrowed to a few people). `hidden` counts what that dropped, so the
    page can offer the way back — booking someone new needs their empty row.
    An explicit pick is never pruned: naming people is itself a request to see
    them, blank week or not.
    """
    rows: dict = {}
    # Notion people properties come back as bare user refs with no name, so
    # pill/row labels for people are resolved against the roster.
    pnames = {p["id"]: p["name"] for p in people}

    def row_for(rid, name):
        return rows.setdefault(rid, {
            "id": rid, "name": name,
            "days": {c: {"total": 0.0, "pills": []} for c in cols},
            "total": 0.0,
        })

    # skeletons first, so people with nothing booked still get a clickable row
    if by == "person":
        for p in people:
            if focus_people and p["id"] not in focus_people:
                continue
            row_for(p["id"], p["name"])
    else:
        for p in projects:
            if focus_project and p["id"] != focus_project:
                continue
            row_for(p["id"], p["name"])

    for a in allocs:
        col = bucket(a["date"])
        if col not in cols:
            continue
        person = pnames.get(a["person_id"], a["person_name"])
        rid = a["person_id"] if by == "person" else a["project_id"]
        label = a["project_name"] if by == "person" else person
        if rid not in rows:
            # an allocation for somebody off the roster (or an archived
            # project) — still show it rather than silently hiding hours
            row_for(rid, person if by == "person" else a["project_name"])
        cell = rows[rid]["days"][col]
        pill = next((p for p in cell["pills"]
                     if p["person_id"] == a["person_id"] and p["project_id"] == a["project_id"]), None)
        if pill:
            pill["hours"] += a["hours"]  # duplicate rows for one pair/day fold into one pill
        else:
            cell["pills"].append({
                "person_id": a["person_id"], "project_id": a["project_id"],
                "label": label, "hours": a["hours"], "swatch": _swatch(a["project_id"]),
            })
        cell["total"] += a["hours"]
        rows[rid]["total"] += a["hours"]

    ordered = sorted(rows.values(), key=lambda r: r["name"].lower())
    for r in ordered:
        for c in cols:
            r["days"][c]["pills"].sort(key=lambda p: (-p["hours"], p["label"].lower()))

    narrowed = (focus_project and not focus_people) if by == "person" else (focus_people and not focus_project)
    hidden = 0
    if narrowed:
        kept = [r for r in ordered if r["total"]]
        hidden = len(ordered) - len(kept)
        ordered = kept
    return ordered, hidden


@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, start: Optional[str] = None, by: str = "person",
                  person: list[str] = Query(default=[]), project: Optional[str] = None,
                  view: str = "days"):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    # Everyone may *read* the planner; only an admin may plan. A normal user is
    # pinned to their own row: the ?person= pick is dropped server-side rather
    # than merely hidden, so a hand-typed id shows nothing. The three write
    # endpoints already refuse non-admins — this is what stops the page from
    # offering them a click that would only 403.
    is_admin = auth.is_admin(user)
    if is_admin:
        # repeated ?person= like /reports — an empty pick means everyone
        picked = [p for p in person if p]
    elif user.get("id"):
        picked = [user["id"]]
    else:
        return RedirectResponse(url="/", status_code=303)   # no identity to scope to
    by = by if by in ("person", "project") else "person"
    view = view if view in ("weeks", "days") else "days"
    anchor = _parse_date(start)  # malformed ?start= falls back to the current week
    mon = ops.monday_of(anchor) if anchor else _current_monday()

    if view == "days":  # the planner: Mon–Fri, weekends never shown
        days = [mon + dt.timedelta(days=i) for i in range(5)]
        cols = [{"iso": d.isoformat(), "top": d.strftime("%a"),
                 "sub": d.strftime("%m/%d"), "href": ""} for d in days]
        range_from, range_to = days[0].isoformat(), days[-1].isoformat()
        def bucket(iso):
            return iso
        cap = _day_target()
        step = dt.timedelta(weeks=1)
    else:  # read-only rollup: six weeks, each column a Monday
        mondays = [mon + dt.timedelta(weeks=i) for i in range(6)]
        base = (f"&by={by}" + "".join(f"&person={p}" for p in picked)
                + (f"&project={project}" if project else ""))
        cols = [{"iso": m.isoformat(), "top": f"W{m.strftime('%m/%d')}", "sub": m.strftime("%Y"),
                 "href": f"/schedule?view=days&start={m.isoformat()}{base}"} for m in mondays]
        range_from = mondays[0].isoformat()
        range_to = (mondays[-1] + dt.timedelta(days=6)).isoformat()
        def bucket(iso):
            return ops.monday_of(dt.date.fromisoformat(iso)).isoformat()
        cap = float(os.environ.get("WEEK_TARGET_HOURS", str(_day_target() * 5)))
        step = dt.timedelta(weeks=6)

    people = ops.list_people()
    projects = ops.list_projects(include_members=True)
    # A multi-person pick still filters in Python (same as /reports): the read
    # is one week of everyone either way. One person is the exception worth
    # pushing into Notion — it's every non-admin's view of this page, so it's
    # the one that got a lot more traffic when the page opened up.
    allocs = ops.alloc_rows(range_from, range_to,
                            picked[0] if len(picked) == 1 else None)
    focus_people = set(picked)
    if focus_people:
        allocs = [a for a in allocs if a["person_id"] in focus_people]
    if project:
        allocs = [a for a in allocs if a["project_id"] == project]
    col_isos = [c["iso"] for c in cols]
    rows, hidden_rows = _schedule_rows(allocs, col_isos, by, bucket, people, projects,
                                       focus_people, project)

    col_totals = {c: sum(r["days"][c]["total"] for r in rows) for c in col_isos}
    return templates.TemplateResponse(request, "schedule.html", {
        "user": user, "by": by, "view": view,
        "cols": cols, "cap": cap, "rows": rows, "col_totals": col_totals,
        "hidden_rows": hidden_rows,
        "grand_total": sum(col_totals.values()),
        "focus_people": picked, "focus_project": project or "",
        "is_admin": is_admin,
        "people": people,
        "projects": [{"id": p["id"], "name": p["name"], "member_ids": p.get("member_ids", []),
                      "swatch": _swatch(p["id"])} for p in projects],
        "prev_start": (mon - step).isoformat(),
        "next_start": (mon + step).isoformat(),
        "this_start": _current_monday().isoformat(),
        "start_iso": mon.isoformat(),
        # "Copy last week" always means the week before the one on screen,
        # whatever `step` is doing for the Earlier/Later nav
        "copy_from": (mon - dt.timedelta(weeks=1)).isoformat(),
        "week_label": mon.strftime("%b %-d"),
        "copy_from_label": (mon - dt.timedelta(weeks=1)).strftime("%b %-d"),
        "last_day": col_isos[-1] if view == "days" else (mon + dt.timedelta(days=4)).isoformat(),
    })


@app.get("/assignments", response_class=HTMLResponse)
def assignments_page(request: Request):
    user = _require_login(request)
    if not user or not auth.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "assignments.html", {
        "user": user,
        "is_admin": True,
        "projects": ops.list_projects(include_members=True),
        "people": ops.list_people(),
    })


class Assignment(BaseModel):
    project_id: str
    person_id: str
    on: bool


@app.post("/api/assignment")
def api_assignment(request: Request, a: Assignment):
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    try:
        ops.set_project_member(a.project_id, a.person_id, a.on)
    except Exception:
        return JSONResponse({"ok": False, "error": "could not save assignment"}, status_code=400)
    return JSONResponse({"ok": True})


class Alloc(BaseModel):
    person_id: str
    project_id: str
    date: str                       # a weekday ISO date
    through: Optional[str] = None   # inclusive end of a repeat range; None = just `date`
    hours: float = Field(ge=0, le=24, allow_inf_nan=False)
    also_assign: bool = True        # scheduling someone implies project membership


_MAX_RANGE_DAYS = 90  # a fat-fingered "through" can't write hundreds of rows


@app.post("/api/allocation")
def api_allocation(request: Request, alloc: Alloc):
    """Assign (or clear, with hours=0) one project for one person across a day
    or a weekday range. Day-first: no week scope, weekends are skipped."""
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    day = _parse_date(alloc.date)
    if not day:
        return JSONResponse({"ok": False, "error": "invalid date"}, status_code=400)
    if day.weekday() >= 5:
        return JSONResponse({"ok": False, "error": "weekday required"}, status_code=400)
    end = _parse_date(alloc.through) if alloc.through else day
    if not end or end < day:
        return JSONResponse({"ok": False, "error": "invalid range"}, status_code=400)
    if (end - day).days > _MAX_RANGE_DAYS:
        return JSONResponse({"ok": False, "error": f"range longer than {_MAX_RANGE_DAYS} days"},
                            status_code=400)
    try:
        res = ops.set_allocation_range(alloc.person_id, alloc.project_id,
                                       day.isoformat(), end.isoformat(), alloc.hours)
    except Exception:
        return JSONResponse({"ok": False, "error": "could not save allocation"}, status_code=400)
    if alloc.hours and alloc.also_assign:
        # keep /assignments honest: booking someone onto a project makes them a
        # member of it (idempotent, so a re-book is a no-op). Deliberately not
        # in the try above: the allocation is already written, so failing the
        # whole response here would show "Save failed" over a saved booking.
        try:
            ops.set_project_member(alloc.project_id, alloc.person_id, True)
            res["assigned"] = True
        except Exception:
            logging.exception("Allocation saved but adding %s to project %s failed",
                              alloc.person_id, alloc.project_id)
    return JSONResponse(res)


class CopyWeek(BaseModel):
    from_start: str                     # any date in the source week
    to_start: str                       # any date in the target week
    person_ids: list[str] = []          # empty = everyone, mirroring the ?person= filter
    project_id: Optional[str] = None


@app.post("/api/allocation/copy-week")
def api_copy_week(request: Request, c: CopyWeek):
    """Duplicate a week of bookings onto another week — "plan next week like
    last week". Additive: nothing in the target week is removed."""
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    src, dst = _parse_date(c.from_start), _parse_date(c.to_start)
    if not src or not dst:
        return JSONResponse({"ok": False, "error": "invalid date"}, status_code=400)
    src, dst = ops.monday_of(src), ops.monday_of(dst)
    if src == dst:
        return JSONResponse({"ok": False, "error": "that's the same week"}, status_code=400)
    if abs((dst - src).days) > 366:
        return JSONResponse({"ok": False, "error": "weeks are more than a year apart"},
                            status_code=400)
    try:
        res = ops.copy_week_allocations(src.isoformat(), dst.isoformat(),
                                        c.person_ids, c.project_id)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception:
        logging.exception("Copying week %s onto %s failed", src, dst)
        return JSONResponse({"ok": False, "error": "could not copy that week"}, status_code=400)
    return JSONResponse(res)


class ClearWeek(BaseModel):
    start: str                          # any date in the week to wipe
    person_ids: list[str] = []          # empty = everyone, mirroring the ?person= filter
    project_id: Optional[str] = None


@app.post("/api/allocation/clear-week")
def api_clear_week(request: Request, c: ClearWeek):
    """Wipe a week of bookings — "start this week over" — honouring the page's
    filters, so it only removes what the planner is showing."""
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    day = _parse_date(c.start)
    if not day:
        return JSONResponse({"ok": False, "error": "invalid date"}, status_code=400)
    mon = ops.monday_of(day)
    try:
        res = ops.clear_week_allocations(mon.isoformat(), c.person_ids, c.project_id)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception:
        logging.exception("Clearing the week of %s failed", mon)
        return JSONResponse({"ok": False, "error": "could not clear that week"}, status_code=400)
    return JSONResponse(res)


class EntryHours(BaseModel):
    entry_id: str
    hours: float = Field(ge=0, le=24, allow_inf_nan=False)


@app.post("/api/entry/hours")
def api_entry_hours(request: Request, e: EntryHours):
    """Correct one logged entry from the report it shows up in (admins).

    /api/cell deliberately writes only the caller's own hours; this is the
    admin counterpart, addressed by entry id so someone else's row can be
    fixed without guessing which of their entries to fold it into.
    """
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not auth.is_admin(user):
        return JSONResponse({"ok": False, "error": "admins only"}, status_code=403)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    try:
        return JSONResponse(ops.set_entry_hours(e.entry_id, e.hours))
    except ValueError:
        return JSONResponse({"ok": False, "error": "not a time entry"}, status_code=400)
    except Exception:
        logging.exception("Editing entry %s failed", e.entry_id)
        return JSONResponse({"ok": False, "error": "could not save that entry"}, status_code=400)


# ---- absences ----------------------------------------------------------
#
# Who is off, and when. Logging one needs no name: it's always the logged-in
# person, so the form is a date (or a range) and a reason. The dashboard reads
# a week or a month at a time, the way /project reads one period rather than a
# window of several.

_ABSENCE_PERIODS = ("weekly", "monthly")


def _absence_columns(period: str, rng: dict) -> list[dict]:
    """The dashboard's columns: the weekdays of a week, or the weeks of a month.

    A month of weekday columns would be 22 of them; bucketing into weeks keeps
    the monthly view readable, and mirrors the schedule's Days/Weeks split.
    """
    first, last = dt.date.fromisoformat(rng["from"]), dt.date.fromisoformat(rng["to"])
    days = ops.weekdays_between(first, last)
    if period == "weekly":
        return [{"key": d.isoformat(), "label": d.strftime("%a"),
                 "sub": d.strftime("%d %b"), "days": [d]} for d in days]
    weeks: dict[dt.date, list[dt.date]] = {}
    for d in days:
        weeks.setdefault(d - dt.timedelta(days=d.weekday()), []).append(d)
    return [{"key": mon.isoformat(), "label": "W" + mon.strftime("%V"),
             "sub": f"{ds[0]:%d} – {ds[-1]:%d %b}", "days": ds}
            for mon, ds in sorted(weeks.items())]


def _absence_days(rows: list[dict], rng: dict) -> dict:
    """(person id) -> {date: reason} for every weekday off *inside* the period.

    An absence that straddles the period edge is clipped here rather than in
    the query, so a fortnight off still counts only the days it costs this
    week — while the row itself stays whole in the list underneath.
    """
    lo, hi = dt.date.fromisoformat(rng["from"]), dt.date.fromisoformat(rng["to"])
    out: dict[str, dict] = {}
    for r in rows:
        start = _parse_date(r["start"])
        end = _parse_date(r["end"]) or start
        if not start:
            continue
        for d in ops.weekdays_between(max(start, lo), min(end, hi)):
            out.setdefault(r["person_id"] or "", {})[d] = r["reason"]
    return out


def _absence_board(rows: list[dict], cols: list[dict], rng: dict,
                   people: list[dict]) -> tuple[list[dict], list[float]]:
    """One row per person who is off in the period, and the column totals."""
    by_person = _absence_days(rows, rng)
    names = {p["id"]: p["name"] for p in people}
    board = []
    for pid, days in by_person.items():
        cells = []
        for c in cols:
            hit = [d for d in c["days"] if d in days]
            cells.append({
                "n": len(hit),
                # the reason belongs on the cell that shows the day off, not
                # only in the list below — hovering a mark should answer "why"
                "why": " · ".join(sorted({days[d] for d in hit if days[d]})),
                "label": ("●" if len(c["days"]) == 1 else str(len(hit))) if hit else "",
            })
        name = next((r["person"] for r in rows if (r["person_id"] or "") == pid), None)
        board.append({"person_id": pid, "person": names.get(pid) or name or "(unassigned)",
                      "cells": cells, "days": len(days)})
    board.sort(key=lambda r: (-r["days"], r["person"].lower()))
    totals = [sum(r["cells"][i]["n"] for r in board) for i in range(len(cols))]
    return board, totals


def _absence_qs(period: str, anchor: str, person_ids: list[str]) -> str:
    qs = f"?period={period}&start={anchor}"
    return qs + "".join(f"&person={pid}" for pid in person_ids)


@app.get("/absences", response_class=HTMLResponse)
def absences_page(request: Request, period: str = "weekly", start: Optional[str] = None,
                  person: list[str] = Query(default=[]),
                  ok: Optional[str] = None, err: Optional[str] = None):
    """Log an absence, and see who's off — a week or a month at a time.

    Everyone sees (and can only remove) their own absences; an admin sees the
    whole team and can filter it, the same scope rule /reports uses.
    """
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    is_admin = auth.is_admin(user)
    period = period if period in _ABSENCE_PERIODS else "weekly"
    rng = _period_range(period, _project_anchor(period, start))
    people = ops.list_people()
    picks = [p for p in person if is_admin]     # the filter is an admin's tool
    rows = ops.list_absences(rng["from"], rng["to"],
                             person_id=None if is_admin else user.get("id"))
    if picks:
        keep = set(picks)
        rows = [r for r in rows if r["person_id"] in keep]
    cols = _absence_columns(period, rng)
    board, totals = _absence_board(rows, cols, rng, people)
    for r in rows:
        s, e = _parse_date(r["start"]), _parse_date(r["end"])
        r["label"] = (f"{s:%d %b %Y}" if s and s == e else
                      f"{s:%d %b} – {e:%d %b %Y}" if s and e else r["start"])
        r["mine"] = bool(user.get("id")) and r["person_id"] == user.get("id")
    rows.sort(key=lambda r: (r["start"], r["person"].lower()))
    return templates.TemplateResponse(request, "absences.html", {
        "user": user, "is_admin": is_admin,
        "enabled": ops.absences_enabled(),
        "period": period, "rng": rng, "anchor": rng["value"],
        "people": people, "focus_people": picks,
        "cols": cols, "board": board, "totals": totals, "rows": rows,
        "days_off": sum(totals), "people_off": len(board),
        "today": dt.date.today().isoformat(),
        "max_reason": ops.MAX_ABSENCE_REASON,
        "ok": ok, "err": err,
    })


@app.post("/absences")
def submit_absence(request: Request,
                   start_date: str = Form(...), end_date: str = Form(""),
                   reason: str = Form(""), period: str = Form("weekly"),
                   anchor: str = Form(""), person: list[str] = Form(default=[])):
    """File one absence for the logged-in person, then land back on the view
    they filed it from."""
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    period = period if period in _ABSENCE_PERIODS else "weekly"
    back = "/absences" + _absence_qs(period, anchor, [p for p in person if auth.is_admin(user)])

    def bounce(err: str = "", ok: str = "") -> RedirectResponse:
        sep = "&" + ("err=" + quote(err) if err else "ok=" + quote(ok))
        return RedirectResponse(url=back + sep, status_code=303)

    if not _same_origin(request):
        return bounce("That request didn't come from this site.")
    first = _parse_date(start_date)
    last = _parse_date(end_date) if end_date else first
    if not first or not last:
        return bounce("That date didn't look like a date.")
    if last < first:
        return bounce("The last day is before the first one.")
    if not reason.strip():
        return bounce("Say why — a word is enough.")
    try:
        row = ops.add_absence(user.get("id"), user.get("name", ""),
                              first.isoformat(), last.isoformat(), reason.strip())
    except ValueError as e:
        return bounce(str(e))
    except Exception:
        logging.exception("Filing an absence for %s failed", user.get("name"))
        return bounce("Notion refused that absence. Try again in a moment.")
    if not row["days"]:   # a Saturday-to-Sunday absence is saved, but costs nothing
        return bounce(ok="Saved — that range is all weekend, so it costs no working days.")
    return bounce(ok=f"{row['days']} day{'' if row['days'] == 1 else 's'} logged as off.")


class AbsenceDelete(BaseModel):
    absence_id: str


@app.post("/api/absence/delete")
def api_absence_delete(request: Request, a: AbsenceDelete):
    """Remove an absence: your own, or anyone's if you're an admin."""
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)
    try:
        ops.delete_absence(a.absence_id, user.get("id"), any_person=auth.is_admin(user))
    except PermissionError:
        return JSONResponse({"ok": False, "error": "that's someone else's absence"},
                            status_code=403)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception:
        logging.exception("Deleting absence %s failed", a.absence_id)
        return JSONResponse({"ok": False, "error": "could not remove that absence"},
                            status_code=400)
    return JSONResponse({"ok": True})


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
