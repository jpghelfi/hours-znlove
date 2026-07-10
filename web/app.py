"""FastAPI web app for the Notion hours tracker.

Notion is the source of truth; this app is a nicer entry form and an editable
Mon–Fri weekly grid on top of it, behind Notion OAuth login (allowlisted).

Run:  ./.venv/bin/uvicorn web.app:app --reload --port 8000
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from . import notion_ops as ops

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="Hours Tracker")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET", "dev-insecure-secret"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    ops.ensure_person_property()


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


@app.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url="/login", status_code=303)


# ---- app pages ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def form_page(request: Request, ok: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "form.html", {
        "user": user,
        "projects": ops.list_projects(),
        "today": dt.date.today().isoformat(),
        "ok": ok,
    })


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
    ops.create_entry(user.get("id"), project_id, date, hours, description)
    return RedirectResponse(url="/?ok=1", status_code=303)


@app.get("/week", response_class=HTMLResponse)
def week_page(request: Request, monday: Optional[str] = None):
    user = _require_login(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    base = dt.date.fromisoformat(monday) if monday else None
    mon = ops.monday_of(base)
    grid = ops.week_grid(mon, user.get("id"))
    return templates.TemplateResponse(request, "week.html", {
        "user": user,
        "grid": grid,
        "projects": ops.list_projects(),
        "prev_mon": (mon - dt.timedelta(days=7)).isoformat(),
        "next_mon": (mon + dt.timedelta(days=7)).isoformat(),
        "this_mon": ops.monday_of().isoformat(),
        "iso_week": mon.strftime("%G-W%V"),
    })


class Cell(BaseModel):
    project_id: str
    date: str
    hours: float
    person_id: Optional[str] = None  # ignored server-side; kept for client compat


@app.post("/api/cell")
def api_cell(request: Request, cell: Cell):
    user = _require_login(request)
    if not user:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    # Always write as the logged-in user — ignore any client-supplied person_id
    # so nobody can edit someone else's hours.
    person_id = user.get("id")
    if not person_id:
        return JSONResponse({"ok": False, "error": "no user identity"}, status_code=400)
    try:
        return JSONResponse(ops.set_cell(person_id, cell.project_id, cell.date, cell.hours))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
