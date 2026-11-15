from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import queries
from app.database import get_db
from app.schemas import ProjectDetail, ProjectList, ProjectTypeTrend

router = APIRouter()


@router.get("/trends", response_model=list[ProjectTypeTrend])
def trends(
    start_date: datetime.date | None = Query(None, description="Only actions filed on/after this date"),
    end_date: datetime.date | None = Query(None, description="Only actions filed on/before this date"),
    db: Session = Depends(get_db),
):
    return queries.get_trends(db, start_date=start_date, end_date=end_date)


@router.get("/projects", response_model=ProjectList)
def projects(
    project_type: str | None = None,
    applicant: str | None = Query(None, description="Case-insensitive substring match"),
    region: str | None = None,
    start_date: datetime.date | None = Query(None, description="Has an action filed on/after this date"),
    end_date: datetime.date | None = Query(None, description="Has an action filed on/before this date"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    total, items = queries.list_projects(
        db,
        project_type=project_type,
        applicant=applicant,
        region=region,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def project_detail(project_id: int, db: Session = Depends(get_db)):
    project = queries.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project
