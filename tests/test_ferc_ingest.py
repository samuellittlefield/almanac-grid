"""Tests for scripts/ingest_ferc.py against a saved sample of FERC search
results (tests/fixtures/sample_ferc_search_response.json). Nothing here talks
to the live FERC site."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from app.models import AgencyAction, Project
from scripts import ingest_ferc
from scripts.ingest_ferc import (
    CP_CONFIG,
    EL_CONFIG,
    P_CONFIG,
    ActionRecord,
    base_docket_number,
    classify_decision,
    extract_applicant,
    extract_project_name,
    extract_region,
    finalize_records,
    fiscal_year_codes,
    merge_application_hit,
    merge_decision_hit,
    merge_withdrawal_hit,
    upsert_records,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_ferc_search_response.json"
WINDOW_START = datetime.date(2016, 1, 1)


@pytest.fixture(scope="module")
def sample() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _parse(sample: dict, config, key: str) -> dict[str, ActionRecord]:
    """Run one docket prefix's saved hits through the same merge steps, in the
    same order, as collect_records_for_prefix."""
    records: dict[str, ActionRecord] = {}
    action_type = config.application_types[0][1]
    for hit in sample[f"{key}_applications"]:
        merge_application_hit(records, hit, config, action_type)
    for hit in sample[f"{key}_orders"]:
        merge_decision_hit(records, hit, config)
    for hit in sample.get(f"{key}_withdrawals", []):
        merge_withdrawal_hit(records, hit, config)
    return {r.docket_number: r for r in finalize_records(records, config, WINDOW_START)}


@pytest.fixture(scope="module")
def cp_records(sample) -> dict[str, ActionRecord]:
    return _parse(sample, CP_CONFIG, "cp")


def test_certificate_application_paired_with_its_order(cp_records):
    record = cp_records["CP17-101-000"]
    assert record.action_type == "certificate_application"
    assert record.project_type == "natural_gas_pipeline"
    assert record.applicant == "Transcontinental Gas Pipe Line Company, LLC"
    assert record.project_name == "Northeast Supply Enhancement Project"
    assert record.filed_date == datetime.date(2017, 3, 27)
    assert record.outcome == "approved"
    assert record.decision_date == datetime.date(2019, 5, 3)
    assert record.source_url == "https://elibrary.ferc.gov/eLibrary/filelist?accession_number=20190503-3049"


def test_procedural_orders_do_not_change_the_outcome(cp_records):
    # "Order Denying Rehearing and Stay" and "Order Vacating Certificate..." sit
    # on the same docket as the approval and must not be read as a denial.
    assert cp_records["CP17-101-000"].outcome == "approved"
    assert "CP17-101-001" not in cp_records
    assert "CP17-101-006" not in cp_records


def test_later_sub_docket_is_a_separate_action(cp_records):
    record = cp_records["CP17-101-007"]
    assert record.action_type == "certificate_amendment"
    # Filed date comes from the petition, not the later supplement.
    assert record.filed_date == datetime.date(2025, 5, 29)
    assert record.outcome == "approved"
    # The order is dated 08/28; the next-day errata notice is not a decision.
    assert record.decision_date == datetime.date(2025, 8, 28)


def test_prior_notice_filings_are_not_applications(cp_records):
    assert "CP19-478-000" not in cp_records


def test_lng_terminal_is_split_out_of_cp_dockets(cp_records):
    record = cp_records["CP20-55-000"]
    assert record.project_type == "lng_terminal"
    assert record.outcome == "approved"
    assert record.project_name == "Example LNG Export Terminal Project"
    assert record.region == "LA"


def test_denied_application(cp_records):
    record = cp_records["CP19-999-000"]
    assert record.outcome == "denied"
    assert record.decision_date == datetime.date(2020, 8, 15)


def test_withdrawn_application(cp_records):
    record = cp_records["CP21-400-000"]
    assert record.outcome == "withdrawn"
    assert record.decision_date == datetime.date(2021, 12, 7)


def test_hydro_relicense_keeps_only_the_decided_application(sample):
    records = _parse(sample, P_CONFIG, "hydro")

    # The draft application and the earlier placeholder filing are precursors
    # of the one relicensing that the 2026 order decided.
    assert list(records) == ["P-2639-028"]
    record = records["P-2639-028"]
    assert record.action_type == "license_application"
    assert record.project_type == "hydroelectric"
    assert record.applicant == "Northern States Power Company - Wisconsin"
    assert record.project_name == "Cornell Project"
    assert record.filed_date == datetime.date(2021, 11, 30)
    assert record.outcome == "approved"
    assert record.decision_date == datetime.date(2026, 9, 10)


@pytest.mark.parametrize(
    "description,expected",
    [
        ("PE Hydro Generation, LLC submits Final License Application for the Warren Hydroelectric Project under P-2391.", True),
        ("Application For License of Fall River Electric Cooperative for the Felt Hydroelectric Project under P-5089.", True),
        ("Appl. For License / Relicense of Xcel Energy Inc. under P-2639.", True),
        ("Benton Falls Associates Application for Approval for Partial Transfer of License for the Benton Falls Hydroelectric Project under P-5073.", False),
        ("Goodyear Lake Hydro, LLC files the Temporary Upstream American Eel Passage Design and Methodology pursuant to Article 401 for the Colliersville Hydroelectric Project under P-2788.", False),
        ("Eugene Water & Electric Board submits the Article 205-As-built Drawings, Request for Amendment of Exhibit A etc. under P-2242.", False),
        ("Northern States Power Company - Wisconsin submits Draft License Application under P-2639.", False),
    ],
)
def test_hydro_license_document_type_is_filtered_to_real_applications(description, expected):
    records: dict[str, ActionRecord] = {}
    hit = {"description": description, "category": "Submittal", "filedDate": "01/03/2022", "docketNumbers": ["P-1-005"]}
    merge_application_hit(records, hit, P_CONFIG, "license_application")
    assert bool(records) is expected


def test_el_complaint(sample):
    records = _parse(sample, EL_CONFIG, "el")

    # Only the original complaint is an action; the later compliance tariff
    # filing on sub-docket 003 is not.
    assert list(records) == ["EL19-47-000"]
    record = records["EL19-47-000"]
    assert record.action_type == "complaint"
    assert record.project_type == "electric_complaint"
    assert record.outcome == "approved"
    assert record.decision_date == datetime.date(2021, 3, 18)


def test_records_outside_the_window_are_dropped(sample):
    records = _parse(sample, CP_CONFIG, "cp")
    late_start = datetime.date(2022, 1, 1)
    kept = finalize_records(dict(records), CP_CONFIG, late_start)
    # Only the 2025 petition was filed or decided on/after 2022. It was filed
    # in two consolidated dockets, so it is an action on both.
    assert sorted(r.docket_number for r in kept) == ["CP17-101-007", "CP20-49-001"]


@pytest.mark.parametrize(
    "description,project_type,expected",
    [
        ("Order Issuing Certificate re X under CP18-1.", "natural_gas_pipeline", "approved"),
        ("Ordering Issuing Certificates re Adelphia Gateway, LLC under CP18-46 et al.", "natural_gas_pipeline", "approved"),
        ("Order on Remand and Reissuing Certificates re Spire STL Pipeline LLC under CP17-40.", "natural_gas_pipeline", "approved"),
        ("Order Addressing Arguments Raised on Rehearing and Denying Stay re X under CP17-40.", "natural_gas_pipeline", None),
        ("Order Granting Extension of Time re X under CP17-101.", "natural_gas_pipeline", None),
        ("Order Issuing Original License re FFP Project 101, LLC's Goldendale Project under P-14861.", "hydroelectric", "approved"),
        ("Order Granting Exemption from Licensing (10 MW or Less) re Water Street Land, LLC under P-14680.", "hydroelectric", "approved"),
        ("Order Issuing Preliminary Permit and Granting Priority to File License Application re X under P-15425.", "hydroelectric", None),
        ("Order Denying Extension of License Term re Ampersand Ogdensburg Hydro, LLC under P-9821.", "hydroelectric", None),
        ("Letter to WEC Energy Group denying the request for an exemption from 18 CFR Part 12 under P-1.", "hydroelectric", None),
        ("Order Denying Complaint re A v. B under EL20-1.", "electric_complaint", "denied"),
        ("Order Granting in Part and Denying in Part Complaint re A v. B under EL20-2.", "electric_complaint", "approved"),
        ("Order on Complaint re A v. B under EL21-61.", "electric_complaint", "outcome_unclear"),
        ("Order On Petition For Declaratory Order re X under EL22-5.", "electric_complaint", "outcome_unclear"),
        ("Order on Complaint and Establishing Hearing and Settlement Judge Procedures re A v. B under EL22-9.", "electric_complaint", None),
        ("Order on Complaint and Section 206 Filing, and Establishing Paper Hearing Procedures re A under EL22-7.", "electric_complaint", None),
        ("Order on Rehearing re Complaint of A under EL21-61.", "electric_complaint", None),
        ("Order on Formula Rate Protocols and Establishing a Show Cause Proceeding re X under EL22-3.", "electric_complaint", None),
        ("Order on Remand re X under CP17-40.", "natural_gas_pipeline", None),
    ],
)
def test_classify_decision(description, project_type, expected):
    assert classify_decision(description, project_type) == expected


def test_extract_applicant_from_order_description():
    hit = {
        "category": "Issuance",
        "description": "Order Issuing New License re Rumford Falls Hydro LLC's Rumford Falls Hydroelectric Project under P-2333.",
    }
    assert extract_applicant(hit) == "Rumford Falls Hydro LLC"
    assert extract_project_name(hit["description"]) == "Rumford Falls Hydroelectric Project"


def test_applicant_name_keeps_its_trailing_period():
    hit = {
        "category": "Submittal",
        "description": "Application of Erie Boulevard Hydropower, L.P. for a new license under P-2713.",
        "affiliations": [{"afType": "AUTHOR", "affiliation": "Erie Boulevard Hydropower, L.P."}],
    }
    assert extract_applicant(hit) == "Erie Boulevard Hydropower, L.P."


def test_extract_region():
    assert extract_region("pipeline facilities in Greene County, Pennsylvania") == "PA"
    assert extract_region("facilities in Kanawha County, West Virginia") == "WV"
    assert extract_region("Order Issuing Certificate re X under CP18-1.") is None
    assert (
        extract_region("construct facilities located in Pennsylvania, Delaware and Maryland re the Delmar Project")
        == "PA,DE,MD"
    )
    assert (
        extract_region("in Greene County, Pennsylvania and Marshall County, West Virginia, and Ohio")
        == "PA,WV,OH"
    )
    # A state in a company name is not a project location.
    assert extract_region("Texas Eastern Transmission, LP submits an application under CP22-1.") is None
    assert extract_region("Complaint of North Iowa Municipal Electric Cooperative Association under EL21-90.") is None


def test_base_docket_number():
    assert base_docket_number("CP18-137-004") == "CP18-137"
    assert base_docket_number("P-2669-102") == "P-2669"


def test_fiscal_year_codes_follow_october_year_start():
    assert fiscal_year_codes(datetime.date(2016, 1, 1), datetime.date(2018, 6, 1)) == ["16", "17", "18"]
    # A docket opened in November 2026 is numbered for fiscal year 2027.
    assert fiscal_year_codes(datetime.date(2025, 1, 1), datetime.date(2026, 11, 15)) == ["25", "26", "27"]


def test_upsert_is_idempotent_and_groups_sub_dockets_under_one_project(db_session, cp_records):
    upsert_records(db_session, cp_records.values())
    db_session.commit()
    upsert_records(db_session, cp_records.values())
    db_session.commit()

    assert db_session.query(Project).count() == 5
    assert db_session.query(AgencyAction).count() == 6

    transco = db_session.query(Project).filter_by(name="Northeast Supply Enhancement Project").one()
    assert transco.applicant == "Transcontinental Gas Pipe Line Company, LLC"
    assert [(a.docket_number, a.action_type, a.outcome) for a in transco.actions] == [
        ("CP17-101-000", "certificate_application", "approved"),
        ("CP17-101-007", "certificate_amendment", "approved"),
    ]
    assert {a.agency for a in transco.actions} == {"FERC"}


@pytest.mark.parametrize("orders", [("unclear", "denied"), ("denied", "unclear")])
def test_known_outcome_beats_an_earlier_unclear_order(orders):
    record = ActionRecord(
        docket_number="EL21-61-000",
        project_type="electric_complaint",
        action_type="complaint",
        filed_date=datetime.date(2021, 1, 4),
    )
    records = {"EL21-61-000": record}
    hits = {
        "unclear": {"description": "Order on Complaint re A v. B under EL21-61.", "filedDate": "06/01/2021",
                    "docketNumbers": ["EL21-61-000"]},
        "denied": {"description": "Order Denying Complaint re A v. B under EL21-61.", "filedDate": "09/01/2021",
                   "docketNumbers": ["EL21-61-000"]},
    }
    for name in orders:
        merge_decision_hit(records, hits[name], EL_CONFIG)

    assert (record.outcome, record.decision_date) == ("denied", datetime.date(2021, 9, 1))


def test_upsert_updates_a_pending_action_once_it_is_decided(db_session):
    pending = ActionRecord(
        docket_number="CP24-7-000",
        project_type="natural_gas_pipeline",
        action_type="certificate_application",
        applicant="Example Pipeline, LLC",
        filed_date=datetime.date(2024, 1, 10),
    )
    upsert_records(db_session, [pending])
    db_session.commit()

    pending.outcome = "approved"
    pending.decision_date = datetime.date(2025, 2, 1)
    upsert_records(db_session, [pending])
    db_session.commit()

    action = db_session.query(AgencyAction).one()
    assert (action.outcome, action.decision_date) == ("approved", datetime.date(2025, 2, 1))


def test_search_splits_the_date_range_instead_of_paging(monkeypatch):
    """FERC's result order is unstable between page requests, so a query that
    overflows one page is split by date until every piece fits in one."""
    filed = [datetime.date(2020, 1, 1) + datetime.timedelta(days=30 * i) for i in range(7)]
    requests_made = []

    def fake_post(session, body):
        search = body["dateSearches"][0]
        start = datetime.date.fromisoformat(search["startDate"])
        end = datetime.date.fromisoformat(search["endDate"])
        requests_made.append(body["curPage"])
        matching = [{"n": i} for i, day in enumerate(filed) if start <= day <= end]
        return {"success": True, "totalHits": len(matching), "searchHits": matching[: body["resultsPerPage"]]}

    monkeypatch.setattr(ingest_ferc, "_post_with_retry", fake_post)
    hits = list(
        ingest_ferc.iter_search_hits(
            None, "P", ["Issuance"], "Order/Opinion", "Delegated Order",
            datetime.date(2020, 1, 1), datetime.date(2020, 12, 31), page_size=3, sleep_seconds=0,
        )
    )
    assert sorted(h["n"] for h in hits) == [0, 1, 2, 3, 4, 5, 6]
    # Every request asked for page 1 (the API's first page; 0 is an alias for it).
    assert set(requests_made) == {1}
