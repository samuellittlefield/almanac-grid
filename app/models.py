from __future__ import annotations

import datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    project_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    applicant: Mapped[str] = mapped_column(String, nullable=False)
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.datetime.utcnow
    )

    actions: Mapped[list["AgencyAction"]] = relationship(
        "AgencyAction",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="AgencyAction.filed_date, AgencyAction.id",
    )


class AgencyAction(Base):
    __tablename__ = "agency_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    agency: Mapped[str] = mapped_column(String, nullable=False, default="FERC")
    docket_number: Mapped[str] = mapped_column(String, nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    filed_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    decision_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    statutory_deadline: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # NEPA review tier: cx / dna / ea / eis / none; NULL = unknown.
    review_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    # True when the action went through an EA or EIS.
    is_major: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())

    project: Mapped["Project"] = relationship("Project", back_populates="actions")


class BlmCaseCache(Base):
    """Raw MLRS case-action list per BLM case, so routine runs only refetch
    cases whose disposition changed in BLM's GIS layers."""

    __tablename__ = "blm_case_cache"

    cse_nr: Mapped[str] = mapped_column(String, primary_key=True)
    sf_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    # The case's disposition and disposition date in the GIS layer when the
    # actions were fetched; a change in either triggers a refetch.
    cse_disp: Mapped[str | None] = mapped_column(String, nullable=True)
    cse_disp_dt: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BlmEplanningCache(Base):
    """Parsed fields of one BLM ePlanning (NEPA register) project page."""

    __tablename__ = "blm_eplanning_cache"

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    # NEPA status from the search listing when the page was fetched; a change
    # triggers a refetch.
    nepa_status: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
