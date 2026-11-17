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
# Project types whose plain label would mislead or read badly.
TYPE_LABELS = {
    "geothermal": "Geothermal (leases)",
    "co2_pipeline": "CO2 pipeline",
    "oil_gas_pipeline": "Oil & gas pipeline",
    "row_grant": "Right-of-way grant",
}


def label(value: str | None) -> str:
    if value in TYPE_LABELS:
        return TYPE_LABELS[value]
    return (value or "").replace("_", " ").capitalize().replace("Lng", "LNG")


templates.env.filters["label"] = label
templates.env.filters["pct"] = lambda value: "—" if value is None else f"{value * 100:.1f}%"

router = APIRouter(include_in_schema=False)


def _parse_agency(value: str | None) -> api.Agency | None:
    try:
        return api.Agency(value) if value else None
    except ValueError:
        return None


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
    agency: str | None = None,
    major_only: str | None = None,
    db: Session = Depends(get_db),
):
    start, end = _parse_date(start_date), _parse_date(end_date)
    agency_filter = _parse_agency(agency)
    # On by default here (unlike the API): BLM pipeline and transmission
    # numbers are dominated by small gathering and distribution lines.
    major = major_only != "false"
    trends = api.trends(start_date=start, end_date=end, agency=agency_filter, major_only=major, db=db)
    api_query = urlencode(
        {
            key: str(value)
            for key, value in {
                "start_date": start,
                "end_date": end,
                "agency": agency_filter.value if agency_filter else None,
                "major_only": "true" if major else None,
            }.items()
            if value
        }
    )
    return templates.TemplateResponse(
        request,
        "trends.html",
        {
            "trends": sorted(trends, key=lambda row: row["agency"]),
            "start_date": start,
            "end_date": end,
            "agency": agency_filter.value if agency_filter else None,
            "major_only": major,
            "api_query": api_query,
            "type_labels": TYPE_LABELS,
        },
    )


@router.get("/ui/projects", response_class=HTMLResponse)
def project_list(
    request: Request,
    project_type: str | None = None,
    applicant: str | None = None,
    region: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    agency: str | None = None,
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    filters = {
        "project_type": project_type or None,
        "applicant": applicant or None,
        "region": region or None,
        "start_date": _parse_date(start_date),
        "end_date": _parse_date(end_date),
        "agency": _parse_agency(agency),
    }
    result = api.projects(**filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, db=db)
    query = urlencode(
        {key: value.value if isinstance(value, api.Agency) else str(value) for key, value in filters.items() if value}
    )
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
