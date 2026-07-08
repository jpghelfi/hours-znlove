"""FastAPI web app for the Notion hours tracker.

Notion is the source of truth; this app is a nicer entry form and an editable
Mon–Fri weekly grid on top of it.

Run:  ./.venv/bin/uvicorn web.app:app --reload --port 8000
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import notion_ops as ops

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="Hours Tracker")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    ops.ensure_person_property()


@app.get("/", response_class=HTMLResponse)
def form_page(request: Request, ok: Optional[str] = None):
    return templates.TemplateResponse("form.html", {
        "request": request,
        "people": ops.list_people(),
        "projects": ops.list_projects(),
        "today": dt.date.today().isoformat(),
        "ok": ok,
    })


@app.post("/entry")
def submit_entry(
    person_id: str = Form(""),
    project_id: str = Form(...),
    date: str = Form(...),
    hours: float = Form(...),
    description: str = Form(""),
):
    ops.create_entry(person_id or None, project_id, date, hours, description)
    return RedirectResponse(url="/?ok=1", status_code=303)


@app.get("/week", response_class=HTMLResponse)
def week_page(request: Request, monday: Optional[str] = None):
    base = dt.date.fromisoformat(monday) if monday else None
    mon = ops.monday_of(base)
    grid = ops.week_grid(mon)
    prev_mon = (mon - dt.timedelta(days=7)).isoformat()
    next_mon = (mon + dt.timedelta(days=7)).isoformat()
    return templates.TemplateResponse("week.html", {
        "request": request,
        "grid": grid,
        "people": ops.list_people(),
        "projects": ops.list_projects(),
        "prev_mon": prev_mon,
        "next_mon": next_mon,
        "this_mon": ops.monday_of().isoformat(),
        "iso_week": mon.strftime("%G-W%V"),
    })


class Cell(BaseModel):
    person_id: str
    project_id: str
    date: str
    hours: float


@app.post("/api/cell")
def api_cell(cell: Cell):
    try:
        result = ops.set_cell(cell.person_id, cell.project_id, cell.date, cell.hours)
        return JSONResponse(result)
    except Exception as exc:  # surface the reason to the client
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
