"""Server-rendered pages. Each page is built from the same handlers that serve
the JSON API (app/api.py), so the UI never has its own data path."""

from __future__ import annotations

import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import api, queries
from app.database import get_db

PAGE_SIZE = 50

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["label"] = lambda value: (value or "").replace("_", " ").capitalize()
templates.env.filters["pct"] = lambda value: "—" if value is None else f"{value * 100:.1f}%"

router = APIRouter(include_in_schema=False)


def _parse_date(value: str | None) -> datetime.date | None:
    """Blank form fields arrive as '' — treat those (and junk) as no filter."""
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("/", response_class=HTMLResponse)
def trends_dashboard(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
):
    start, end = _parse_date(start_date), _parse_date(end_date)
    trends = api.trends(start_date=start, end_date=end, db=db)
    return templates.TemplateResponse(
        request,
        "trends.html",
        {"trends": trends, "start_date": start, "end_date": end},
    )


@router.get("/ui/projects", response_class=HTMLResponse)
def project_list(
    request: Request,
    project_type: str | None = None,
    applicant: str | None = None,
    region: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    filters = {
        "project_type": project_type or None,
        "applicant": applicant or None,
        "region": region or None,
        "start_date": _parse_date(start_date),
        "end_date": _parse_date(end_date),
    }
    result = api.projects(**filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, db=db)
    query = urlencode({key: str(value) for key, value in filters.items() if value})
    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "result": result,
            "filters": filters,
            "options": queries.list_filter_options(db),
            "page": page,
            "pages": max(1, -(-result["total"] // PAGE_SIZE)),
            "query": query,
        },
    )


@router.get("/ui/projects/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: int, db: Session = Depends(get_db)):
    project = api.project_detail(project_id, db=db)
    return templates.TemplateResponse(request, "project_detail.html", {"project": project})
