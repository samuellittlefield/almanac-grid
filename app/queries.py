from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import AgencyAction, Project

DECIDED_OUTCOMES = ("approved", "denied", "withdrawn")


def _apply_filed_range(stmt, start_date: datetime.date | None, end_date: datetime.date | None):
    if start_date is not None:
        stmt = stmt.where(AgencyAction.filed_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(AgencyAction.filed_date <= end_date)
    return stmt


def _rate(count: int, decided: int) -> float | None:
    return round(count / decided, 4) if decided else None


def _summarize(rows) -> dict:
    """rows: iterable of (outcome, filed_date, decision_date)."""
    counts = {"approved": 0, "denied": 0, "withdrawn": 0, "pending": 0}
    durations: list[int] = []
    for outcome, filed, decided in rows:
        counts[outcome if outcome in counts else "pending"] += 1
        if outcome in DECIDED_OUTCOMES and filed and decided and decided >= filed:
            durations.append((decided - filed).days)
    decided_total = sum(counts[o] for o in DECIDED_OUTCOMES)
    return {
        "total_actions": decided_total + counts["pending"],
        "decided": decided_total,
        **counts,
        "approval_rate": _rate(counts["approved"], decided_total),
        "denial_rate": _rate(counts["denied"], decided_total),
        "withdrawal_rate": _rate(counts["withdrawn"], decided_total),
        "avg_days_to_decision": round(sum(durations) / len(durations), 1) if durations else None,
    }


def get_trends(
    db: Session,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
) -> list[dict]:
    """Outcome rates and time-to-decision by project_type, with a per-year
    breakdown keyed on the year each action was filed."""
    stmt = select(
        Project.project_type,
        AgencyAction.outcome,
        AgencyAction.filed_date,
        AgencyAction.decision_date,
    ).join(AgencyAction, AgencyAction.project_id == Project.id)
    stmt = _apply_filed_range(stmt, start_date, end_date)

    by_type: dict[str, list] = {}
    for project_type, outcome, filed, decided in db.execute(stmt):
        by_type.setdefault(project_type, []).append((outcome, filed, decided))

    trends = []
    for project_type in sorted(by_type):
        rows = by_type[project_type]
        by_year: dict[int, list] = {}
        for row in rows:
            if row[1] is not None:
                by_year.setdefault(row[1].year, []).append(row)
        trends.append(
            {
                "project_type": project_type,
                **_summarize(rows),
                "by_year": [{"year": year, **_summarize(by_year[year])} for year in sorted(by_year)],
            }
        )
    return trends


def list_projects(
    db: Session,
    project_type: str | None = None,
    applicant: str | None = None,
    region: str | None = None,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[Project]]:
    stmt = select(Project)
    if project_type:
        stmt = stmt.where(Project.project_type == project_type)
    if applicant:
        stmt = stmt.where(Project.applicant.ilike(f"%{applicant}%"))
    if region:
        stmt = stmt.where(func.lower(Project.region) == region.lower())
    if start_date is not None or end_date is not None:
        in_range = _apply_filed_range(select(AgencyAction.project_id), start_date, end_date)
        stmt = stmt.where(Project.id.in_(in_range))

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    projects = db.scalars(
        stmt.options(selectinload(Project.actions))
        .order_by(Project.name, Project.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return total or 0, list(projects)


def get_project(db: Session, project_id: int) -> Project | None:
    return db.scalars(
        select(Project).options(selectinload(Project.actions)).where(Project.id == project_id)
    ).one_or_none()


def list_filter_options(db: Session) -> dict[str, list[str]]:
    types = db.scalars(select(Project.project_type).distinct().order_by(Project.project_type)).all()
    regions = db.scalars(
        select(Project.region).where(Project.region.is_not(None)).distinct().order_by(Project.region)
    ).all()
    return {"project_types": list(types), "regions": list(regions)}
