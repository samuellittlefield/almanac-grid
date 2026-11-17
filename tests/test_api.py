from __future__ import annotations

import datetime

import pytest

from app.models import AgencyAction, Project


def _add(db, name, project_type, applicant, region, actions):
    project = Project(name=name, project_type=project_type, applicant=applicant, region=region)
    db.add(project)
    db.flush()
    for docket, action_type, filed, decided, outcome in actions:
        db.add(
            AgencyAction(
                project_id=project.id,
                docket_number=docket,
                action_type=action_type,
                filed_date=filed,
                decision_date=decided,
                outcome=outcome,
                source_url=f"https://elibrary.ferc.gov/eLibrary/filelist?accession_number={docket}",
            )
        )
    return project


@pytest.fixture()
def seeded(db_session):
    d = datetime.date
    projects = {
        "alpha": _add(
            db_session, "Alpha Lateral Project", "natural_gas_pipeline", "Alpha Pipeline, LLC", "TX",
            [
                ("CP19-1-000", "certificate_application", d(2019, 1, 1), d(2019, 4, 11), "approved"),
                ("CP19-1-002", "certificate_amendment", d(2021, 6, 1), None, "pending"),
            ],
        ),
        "bravo": _add(
            db_session, "Bravo Loop Project", "natural_gas_pipeline", "Bravo Gas Company", "PA",
            [("CP20-2-000", "certificate_application", d(2020, 3, 1), d(2020, 9, 17), "denied")],
        ),
        "charlie": _add(
            db_session, "Charlie Expansion Project", "natural_gas_pipeline", "Alpha Pipeline, LLC", None,
            [("CP20-3-000", "certificate_application", d(2020, 5, 1), d(2020, 7, 30), "withdrawn")],
        ),
        "delta": _add(
            db_session, "Delta Falls Project", "hydroelectric", "Delta Hydro LLC", "WA",
            [("P-100-050", "license_application", d(2018, 2, 1), d(2021, 2, 1), "approved")],
        ),
    }
    db_session.commit()
    return projects


def test_trends_empty_database(client):
    response = client.get("/trends")
    assert response.status_code == 200
    assert response.json() == []


def test_trends_groups_by_project_type(client, seeded):
    trends = {row["project_type"]: row for row in client.get("/trends").json()}

    assert set(trends) == {"hydroelectric", "natural_gas_pipeline"}
    gas = trends["natural_gas_pipeline"]
    assert (gas["total_actions"], gas["decided"], gas["pending"]) == (4, 3, 1)
    assert (gas["approved"], gas["denied"], gas["withdrawn"]) == (1, 1, 1)
    assert gas["approval_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert gas["denial_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert gas["withdrawal_rate"] == pytest.approx(1 / 3, abs=1e-4)
    # 100, 200 and 90 days.
    assert gas["avg_days_to_decision"] == 130.0

    hydro = trends["hydroelectric"]
    assert hydro["approval_rate"] == 1.0
    assert hydro["avg_days_to_decision"] == 1096.0


def test_trends_by_year_breakdown(client, seeded):
    trends = {row["project_type"]: row for row in client.get("/trends").json()}
    by_year = {row["year"]: row for row in trends["natural_gas_pipeline"]["by_year"]}

    assert sorted(by_year) == [2019, 2020, 2021]
    assert (by_year[2020]["denied"], by_year[2020]["withdrawn"], by_year[2020]["approved"]) == (1, 1, 0)
    assert by_year[2020]["avg_days_to_decision"] == 145.0
    assert by_year[2021]["pending"] == 1
    assert by_year[2021]["approval_rate"] is None


def test_trends_date_range_filter(client, seeded):
    response = client.get("/trends", params={"start_date": "2020-01-01", "end_date": "2020-12-31"})
    trends = response.json()

    assert [row["project_type"] for row in trends] == ["natural_gas_pipeline"]
    assert (trends[0]["total_actions"], trends[0]["approved"], trends[0]["denied"]) == (2, 0, 1)


def test_trends_outcome_unclear_counts_toward_timing_not_rates(client, db_session):
    d = datetime.date
    _add(
        db_session, "A v. B", "electric_complaint", "A", None,
        [
            ("EL21-1-000", "complaint", d(2021, 1, 1), d(2021, 3, 2), "denied"),
            ("EL21-2-000", "complaint", d(2021, 1, 1), d(2021, 5, 1), "outcome_unclear"),
            ("EL21-3-000", "complaint", d(2021, 1, 1), None, "pending"),
        ],
    )
    db_session.commit()

    el = client.get("/trends").json()[0]

    assert (el["total_actions"], el["decided"], el["outcome_unclear"], el["pending"]) == (3, 1, 1, 1)
    assert el["denial_rate"] == 1.0
    # 60 and 120 days.
    assert el["avg_days_to_decision"] == 90.0


def test_trends_rejects_bad_date(client):
    assert client.get("/trends", params={"start_date": "not-a-date"}).status_code == 422


def test_projects_list(client, seeded):
    body = client.get("/projects").json()

    assert body["total"] == 4
    assert [item["name"] for item in body["items"]] == [
        "Alpha Lateral Project",
        "Bravo Loop Project",
        "Charlie Expansion Project",
        "Delta Falls Project",
    ]
    assert body["items"][0]["project_type"] == "natural_gas_pipeline"


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"project_type": "hydroelectric"}, ["Delta Falls Project"]),
        ({"applicant": "alpha pipeline"}, ["Alpha Lateral Project", "Charlie Expansion Project"]),
        ({"region": "pa"}, ["Bravo Loop Project"]),
        ({"start_date": "2021-01-01"}, ["Alpha Lateral Project"]),
        ({"end_date": "2018-12-31"}, ["Delta Falls Project"]),
        ({"start_date": "2020-01-01", "end_date": "2020-03-31"}, ["Bravo Loop Project"]),
        ({"project_type": "natural_gas_pipeline", "region": "TX"}, ["Alpha Lateral Project"]),
        ({"applicant": "nobody"}, []),
    ],
)
def test_projects_filters(client, seeded, params, expected):
    body = client.get("/projects", params=params).json()
    assert [item["name"] for item in body["items"]] == expected
    assert body["total"] == len(expected)


def test_projects_pagination(client, seeded):
    body = client.get("/projects", params={"limit": 2, "offset": 2}).json()
    assert body["total"] == 4
    assert [item["name"] for item in body["items"]] == ["Charlie Expansion Project", "Delta Falls Project"]


def test_project_detail_includes_action_history(client, seeded):
    body = client.get(f"/projects/{seeded['alpha'].id}").json()

    assert body["name"] == "Alpha Lateral Project"
    assert body["applicant"] == "Alpha Pipeline, LLC"
    assert [(a["docket_number"], a["action_type"], a["outcome"]) for a in body["actions"]] == [
        ("CP19-1-000", "certificate_application", "approved"),
        ("CP19-1-002", "certificate_amendment", "pending"),
    ]
    first = body["actions"][0]
    assert first["agency"] == "FERC"
    assert (first["filed_date"], first["decision_date"]) == ("2019-01-01", "2019-04-11")
    assert first["statutory_deadline"] is None
    assert first["source_url"].startswith("https://elibrary.ferc.gov/")


def test_project_detail_not_found(client):
    assert client.get("/projects/999").status_code == 404
