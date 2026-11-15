from __future__ import annotations

import datetime

from app.models import AgencyAction, Project


def test_create_project_with_agency_action(db_session):
    project = Project(
        name="Example Pipeline Project",
        project_type="natural_gas_pipeline",
        applicant="Example Pipeline, LLC",
        region="LA",
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    action = AgencyAction(
        project_id=project.id,
        agency="FERC",
        docket_number="CP20-1",
        action_type="certificate_application",
        filed_date=datetime.date(2020, 1, 15),
        decision_date=datetime.date(2021, 3, 1),
        outcome="approved",
        source_url="https://elibrary.ferc.gov/eLibrary/filelist?accession_number=20200115-5000",
    )
    db_session.add(action)
    db_session.commit()
    db_session.refresh(action)

    assert project.id is not None
    assert action.id is not None
    assert action.project_id == project.id
    assert action.project.name == "Example Pipeline Project"
    assert project.actions[0].docket_number == "CP20-1"


def test_agency_action_defaults(db_session):
    project = Project(
        name="Example Hydro Project",
        project_type="hydroelectric",
        applicant="Example Hydro, LLC",
        region="WA",
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    action = AgencyAction(
        project_id=project.id,
        docket_number="P-12345",
        action_type="license_application",
        filed_date=datetime.date(2022, 6, 1),
    )
    db_session.add(action)
    db_session.commit()
    db_session.refresh(action)

    assert action.agency == "FERC"
    assert action.outcome == "pending"
    assert action.decision_date is None
