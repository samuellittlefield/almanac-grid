"""Agency and major-only filters across the API and pages, with FERC and BLM
rows side by side."""

from __future__ import annotations

import datetime

import pytest

from app.models import AgencyAction, Project
from scripts.enrich_regions import apply_regions

d = datetime.date


def _add(db, name, project_type, agency, docket, filed, decided, outcome, *, is_major=False, review_tier=None, region=None):
    project = Project(name=name, project_type=project_type, applicant=f"{name} LLC", region=region)
    db.add(project)
    db.flush()
    db.add(
        AgencyAction(
            project_id=project.id,
            agency=agency,
            docket_number=docket,
            action_type="row_grant" if agency == "BLM" else "certificate_application",
            filed_date=filed,
            decision_date=decided,
            outcome=outcome,
            is_major=is_major,
            review_tier=review_tier,
            source_url="https://mlrs.blm.gov/s/blm-case/a02x/x" if agency == "BLM" else None,
        )
    )
    return project


@pytest.fixture()
def mixed(db_session):
    projects = {
        "ferc_gas": _add(db_session, "Gas Lateral", "natural_gas_pipeline", "FERC", "CP19-1-000", d(2019, 1, 1), d(2019, 6, 1), "approved"),
        "solar": _add(db_session, "Sun Field", "solar", "BLM", "NVNV105000001", d(2020, 1, 1), d(2021, 1, 1), "approved", is_major=True, review_tier="eis", region="NV"),
        "solar_denied": _add(db_session, "Sun Two", "solar", "BLM", "NVNV105000002", d(2020, 2, 1), d(2020, 8, 1), "denied", region="NV"),
        "big_line": _add(db_session, "Big Line", "oil_gas_pipeline", "BLM", "WYWY105000003", d(2019, 3, 1), d(2020, 3, 1), "approved", is_major=True, review_tier="ea", region="WY"),
        "small_line": _add(db_session, "Small Line", "oil_gas_pipeline", "BLM", "WYWY105000004", d(2019, 4, 1), d(2019, 5, 1), "approved", review_tier="cx", region="WY"),
        "lease": _add(db_session, "Hot Springs", "geothermal", "BLM", "NVNV105000005", d(2018, 1, 1), None, "pending", region="NV"),
    }
    db_session.commit()
    return projects


def _by_type(rows):
    return {row["project_type"]: row for row in rows}


def test_trends_rows_carry_their_agency(client, mixed):
    rows = _by_type(client.get("/trends").json())
    assert rows["natural_gas_pipeline"]["agency"] == "FERC"
    assert rows["solar"]["agency"] == "BLM"
    assert rows["solar"]["approved"] == 1 and rows["solar"]["denied"] == 1
    assert rows["oil_gas_pipeline"]["total_actions"] == 2


def test_trends_agency_filter(client, mixed):
    assert set(_by_type(client.get("/trends", params={"agency": "FERC"}).json())) == {"natural_gas_pipeline"}
    assert set(_by_type(client.get("/trends", params={"agency": "BLM"}).json())) == {"solar", "oil_gas_pipeline", "geothermal"}
    assert client.get("/trends", params={"agency": "EPA"}).status_code == 422


def test_trends_major_only_trims_blm_line_types_only(client, mixed):
    rows = _by_type(client.get("/trends", params={"major_only": "true"}).json())
    assert rows["oil_gas_pipeline"]["total_actions"] == 1  # the CX gathering line is dropped
    assert rows["solar"]["total_actions"] == 2  # solar isn't a noisy line type
    assert rows["natural_gas_pipeline"]["total_actions"] == 1  # FERC is untouched
    assert rows["geothermal"]["total_actions"] == 1


def test_trends_default_is_unchanged_for_ferc(client, mixed):
    everything = _by_type(client.get("/trends").json())
    ferc_only = _by_type(client.get("/trends", params={"agency": "FERC"}).json())
    assert everything["natural_gas_pipeline"] == ferc_only["natural_gas_pipeline"]


def test_projects_agency_filter(client, mixed):
    blm = client.get("/projects", params={"agency": "BLM"}).json()
    ferc = client.get("/projects", params={"agency": "FERC"}).json()
    assert blm["total"] == 5 and ferc["total"] == 1
    assert ferc["items"][0]["name"] == "Gas Lateral"
    both = client.get("/projects", params={"agency": "BLM", "region": "WY"}).json()
    assert {item["name"] for item in both["items"]} == {"Big Line", "Small Line"}


def test_project_detail_exposes_review_tier(client, mixed):
    body = client.get(f"/projects/{mixed['solar'].id}").json()
    action = body["actions"][0]
    assert (action["agency"], action["review_tier"], action["is_major"]) == ("BLM", "eis", True)


def test_dashboard_defaults_to_major_only_with_toggle(client, mixed):
    page = client.get("/").text
    assert 'value="true" selected' in page
    assert "Oil &amp; gas pipeline" in page and "Small Line" not in page
    assert "major_only=true" in page  # charts fetch the same view
    assert 'value="false" selected' in client.get("/", params={"major_only": "false"}).text


def test_dashboard_agency_filter_and_labels(client, mixed):
    page = client.get("/", params={"agency": "BLM"}).text
    assert "Geothermal (leases)" in page
    assert "Natural gas pipeline" not in page
    assert "<td>BLM</td>" in page and "<td>FERC</td>" not in page
    both = client.get("/").text
    assert "<td>BLM</td>" in both and "<td>FERC</td>" in both


def test_project_pages_show_agency_serial_and_mlrs_link(client, mixed):
    listing = client.get("/ui/projects", params={"agency": "BLM"}).text
    assert "Sun Field" in listing and "Gas Lateral" not in listing
    detail = client.get(f"/ui/projects/{mixed['solar'].id}").text
    assert "NVNV105000001" in detail and "MLRS" in detail and "EIS" in detail
    assert "Right-of-way grant" in detail
    lease = client.get(f"/ui/projects/{mixed['lease'].id}").text
    assert "Geothermal (leases)" in lease and "isn't, the power plant" in lease


def test_apply_regions_ignores_blm_actions(db_session):
    blm = _add(db_session, "Line", "oil_gas_pipeline", "BLM", "P-2639-028", None, None, "pending", region="WY")
    ferc = _add(db_session, "Dam", "hydroelectric", "FERC", "P-2639-028", None, None, "pending")
    db_session.flush()
    assert apply_regions(db_session, {"P-2639": ["WI"]}) == 1
    assert (blm.region, ferc.region) == ("WY", "WI")
