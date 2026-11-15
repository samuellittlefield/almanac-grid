from __future__ import annotations

import datetime

import pytest

from app.models import AgencyAction, Project


@pytest.fixture()
def project(db_session):
    project = Project(
        name="Alpha Lateral Project",
        project_type="natural_gas_pipeline",
        applicant="Alpha Pipeline, LLC",
        region="TX",
    )
    db_session.add(project)
    db_session.flush()
    db_session.add(
        AgencyAction(
            project_id=project.id,
            docket_number="CP19-1-000",
            action_type="certificate_application",
            filed_date=datetime.date(2019, 1, 1),
            decision_date=datetime.date(2019, 4, 11),
            outcome="approved",
            source_url="https://elibrary.ferc.gov/eLibrary/filelist?accession_number=20190411-3000",
        )
    )
    db_session.commit()
    return project


def test_trends_dashboard_renders_database_rows(client, project):
    response = client.get("/")
    assert response.status_code == 200
    assert "Natural gas pipeline" in response.text
    assert "100.0%" in response.text
    assert "chart.js" in response.text


def test_trends_dashboard_empty_state(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "scripts.ingest_ferc" in response.text


def test_trends_dashboard_date_filter_excludes_rows(client, project):
    response = client.get("/", params={"start_date": "2022-01-01", "end_date": ""})
    assert response.status_code == 200
    assert "Natural gas pipeline" not in response.text


def test_project_list_page(client, project):
    response = client.get("/ui/projects")
    assert response.status_code == 200
    assert "Alpha Lateral Project" in response.text
    assert f"/ui/projects/{project.id}" in response.text
    assert "CP19-1-000" in response.text


def test_project_list_page_filters(client, project):
    assert "Alpha Lateral Project" in client.get("/ui/projects", params={"applicant": "alpha"}).text
    filtered = client.get("/ui/projects", params={"project_type": "hydroelectric", "applicant": ""})
    assert "Alpha Lateral Project" not in filtered.text
    assert "No projects match" in filtered.text


def test_project_detail_page(client, project):
    response = client.get(f"/ui/projects/{project.id}")
    assert response.status_code == 200
    assert "Alpha Pipeline, LLC" in response.text
    assert "CP19-1-000" in response.text
    assert "Certificate application" in response.text
    assert "accession_number=20190411-3000" in response.text
    assert ">100<" in response.text


def test_project_detail_page_not_found(client):
    assert client.get("/ui/projects/999").status_code == 404
