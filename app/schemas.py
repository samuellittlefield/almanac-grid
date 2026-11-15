from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict


class AgencyActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agency: str
    docket_number: str
    action_type: str
    filed_date: datetime.date | None
    decision_date: datetime.date | None
    outcome: str
    statutory_deadline: datetime.date | None
    source_url: str | None


class ProjectSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    project_type: str
    applicant: str
    region: str | None
    created_at: datetime.datetime


class ProjectDetail(ProjectSummary):
    actions: list[AgencyActionOut]


class ProjectList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ProjectSummary]


class OutcomeSummary(BaseModel):
    total_actions: int
    decided: int
    approved: int
    denied: int
    withdrawn: int
    pending: int
    approval_rate: float | None
    denial_rate: float | None
    withdrawal_rate: float | None
    avg_days_to_decision: float | None


class YearTrend(OutcomeSummary):
    year: int


class ProjectTypeTrend(OutcomeSummary):
    project_type: str
    by_year: list[YearTrend]
