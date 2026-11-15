from __future__ import annotations

import datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String
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

    project: Mapped["Project"] = relationship("Project", back_populates="actions")
