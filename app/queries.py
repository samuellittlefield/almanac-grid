from __future__ import annotations

import datetime

from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import AgencyAction, Project

DECIDED_OUTCOMES = ("approved", "denied", "withdrawn")
# Decided on a known date but in an unknown direction: counts toward time to
# decision, not toward the approval/denial/withdrawal rates.
TIMED_OUTCOMES = DECIDED_OUTCOMES + ("outcome_unclear",)
AGENCIES = ("FERC", "BLM")
# BLM line types where thousands of small gathering / distribution lines swamp
# the handful of real projects. `major_only` restricts these to actions that
# went through an EA or EIS; other types have no such noise and are left alone.
MAJOR_FILTER_TYPES = ("electric_transmission", "oil_gas_pipeline", "co2_pipeline")


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
    counts = {"approved": 0, "denied": 0, "withdrawn": 0, "outcome_unclear": 0, "pending": 0}
    durations: list[int] = []
    for outcome, filed, decided in rows:
        counts[outcome if outcome in counts else "pending"] += 1
        if outcome in TIMED_OUTCOMES and filed and decided and decided >= filed:
            durations.append((decided - filed).days)
    decided_total = sum(counts[o] for o in DECIDED_OUTCOMES)
    return {
        "total_actions": decided_total + counts["outcome_unclear"] + counts["pending"],
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
    agency: str | None = None,
    major_only: bool = False,
) -> list[dict]:
    """Outcome rates and time-to-decision by project_type (and the agency
    that decides it), with a per-year breakdown keyed on the year each action
    was filed."""
    stmt = select(
        Project.project_type,
        AgencyAction.agency,
        AgencyAction.outcome,
        AgencyAction.filed_date,
        AgencyAction.decision_date,
    ).join(AgencyAction, AgencyAction.project_id == Project.id)
    stmt = _apply_filed_range(stmt, start_date, end_date)
    if agency:
        stmt = stmt.where(AgencyAction.agency == agency)
    if major_only:
        stmt = stmt.where(
            or_(
                AgencyAction.agency != "BLM",
                Project.project_type.not_in(MAJOR_FILTER_TYPES),
                AgencyAction.is_major.is_(True),
            )
        )

    by_type: dict[tuple[str, str], list] = {}
    for project_type, action_agency, outcome, filed, decided in db.execute(stmt):
        by_type.setdefault((project_type, action_agency), []).append((outcome, filed, decided))

    trends = []
    for project_type, action_agency in sorted(by_type):
        rows = by_type[(project_type, action_agency)]
        by_year: dict[int, list] = {}
        for row in rows:
            if row[1] is not None:
                by_year.setdefault(row[1].year, []).append(row)
        trends.append(
            {
                "project_type": project_type,
                "agency": action_agency,
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
    agency: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[Project]]:
    stmt = select(Project)
    if project_type:
        stmt = stmt.where(Project.project_type == project_type)
    if applicant:
        stmt = stmt.where(Project.applicant.ilike(f"%{applicant}%"))
    if region:
        # region is a comma-separated list of state codes ("NY,PA").
        padded = func.lower(literal(",") + Project.region + literal(","))
        stmt = stmt.where(padded.contains(f",{region.strip().lower()},", autoescape=True))
    if start_date is not None or end_date is not None or agency:
        matching = _apply_filed_range(select(AgencyAction.project_id), start_date, end_date)
        if agency:
            matching = matching.where(AgencyAction.agency == agency)
        stmt = stmt.where(Project.id.in_(matching))

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
    region_lists = db.scalars(select(Project.region).where(Project.region.is_not(None)).distinct()).all()
    regions = sorted({code for value in region_lists for code in value.split(",") if code})
    agencies = db.scalars(select(AgencyAction.agency).distinct().order_by(AgencyAction.agency)).all()
    return {"project_types": list(types), "regions": regions, "agencies": list(agencies)}
