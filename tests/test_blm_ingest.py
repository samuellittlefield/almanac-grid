from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.models import AgencyAction, BlmCaseCache, BlmEplanningCache, Project
from scripts import eplanning, ingest_blm, mlrs
from scripts.ingest_blm import (
    BlmCase,
    Decision,
    PRODUCT_GROUPS,
    build_records,
    collapse_parcels,
    decode_actions,
    in_window,
    refresh_case_cache,
    resolve_outcome,
    upsert_records,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOLAR = next(group for group in PRODUCT_GROUPS if group.project_type == "solar")
START = date(2016, 1, 1)


def _action(code, when, status="Migrated Case Action", name=""):
    return {"Code__c": code, "Action_Name__c": name, "Status__c": status, "Action_Decision_Date__c": when}


def _case(**overrides) -> BlmCase:
    values = dict(
        cse_nr="AZAZ105846925",
        project_type="solar",
        action_type="row_grant",
        product="Solar Development Grant",
        disposition="Closed",
        sf_id="a023d00000Ga2HoAAJ",
        legacy_serial="AZA 03418702",
        cse_disp_dt=date(2021, 5, 4),
        customer="SONORAN SOLAR ENERGY LLC",
        states=["AZ"],
    )
    values.update(overrides)
    return BlmCase(**values)


# --- Source 1: GIS layers -------------------------------------------------


def _fixture_rows():
    body = json.loads((FIXTURES / "blm_arcgis_closed_solar.json").read_text())
    return [("Closed", feature["attributes"]) for feature in body["features"]]


def test_collapse_parcels_from_saved_layer_response():
    cases = collapse_parcels(_fixture_rows(), SOLAR)
    case = cases["AZAZ105846925"]
    assert case.sf_id == "a023d00000Ga2HoAAJ"
    assert case.legacy_serial == "AZA 03418702"
    assert case.customer == "SONORAN SOLAR ENERGY LLC"
    assert case.states == ["AZ"]
    assert case.disposition == "Closed"
    assert case.cse_disp_dt is not None
    assert case.project_type == "solar" and case.action_type == "row_grant"


def test_collapse_parcels_merges_parcels_of_one_case():
    base = {"CSE_NR": "NVNV105000001", "SF_ID": "a02x", "BLM_PROD": "Solar Development Grant", "CSE_DISP_DT": 1609459200000}
    rows = [
        ("Pending", {**base, "GEO_STATE": "NV", "CUST_NM_SEC": "LANDMAN LLC", "INT_REL_SEC": "Agent", "PCT_INT_SEC": 0}),
        ("Pending", {**base, "GEO_STATE": "CA", "CUST_NM_SEC": "SUNNY SOLAR LLC", "INT_REL_SEC": "Applicant", "PCT_INT_SEC": 100}),
        ("Pending", {**base, "GEO_STATE": "NV", "CUST_NM_SEC": None, "INT_REL_SEC": None, "PCT_INT_SEC": None}),
        ("Pending", {**base, "CSE_NR": "NVNV105000002", "GEO_STATE": "NV"}),
    ]
    cases = collapse_parcels(rows, SOLAR)
    assert set(cases) == {"NVNV105000001", "NVNV105000002"}
    case = cases["NVNV105000001"]
    assert case.states == ["NV", "CA"]
    assert case.customer == "SUNNY SOLAR LLC"
    assert case.cse_disp_dt == date(2021, 1, 1)


def test_collapse_parcels_keeps_latest_disposition_across_layers():
    base = {"CSE_NR": "WYWY105000003", "BLM_PROD": "Wind Development Grant", "GEO_STATE": "WY"}
    rows = [
        ("Pending", {**base, "CSE_DISP_DT": 1500000000000}),
        ("Authorized", {**base, "CSE_DISP_DT": 1600000000000}),
    ]
    assert collapse_parcels(rows, SOLAR)["WYWY105000003"].disposition == "Authorized"


class FakeArcgis:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        body = self.body if url.endswith("/3/query") and params["resultOffset"] == 0 else {"features": []}
        return FakeResponse(json.dumps(body))


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return json.loads(self.text)


def test_fetch_cases_queries_every_layer_with_window_on_decided_ones():
    body = json.loads((FIXTURES / "blm_arcgis_closed_solar.json").read_text())
    session = FakeArcgis(body)
    cases = ingest_blm.fetch_cases(session, START, ["solar"], log=lambda _: None)
    assert {case.cse_nr for case in cases} == {f["attributes"]["CSE_NR"] for f in body["features"]}
    wheres = {url.rsplit("/", 2)[-2]: params["where"] for url, params in session.calls}
    assert "CSE_DISP_DT >= DATE '2016-01-01'" in wheres["0"]  # Authorized
    assert "CSE_DISP_DT >= DATE '2016-01-01'" in wheres["3"]  # Closed
    assert "CSE_DISP_DT" not in wheres["1"]  # Pending: everything
    assert "'Solar Development Lease'" in wheres["1"]


# --- Source 2: MLRS actions -> outcome -------------------------------------


def test_saved_mlrs_response_decodes_to_denial():
    body = json.loads((FIXTURES / "mlrs_related_list_records.json").read_text())
    records, next_token = mlrs.parse_records_response(body["actions"][0])
    assert next_token is None
    assert {record["Code__c"] for record in records} >= {"124", "125", "970"}
    decision = decode_actions(records)
    assert decision == Decision(date(2007, 5, 21), "denied", date(2007, 11, 27))


@pytest.mark.parametrize(
    "actions, expected",
    [
        # Granted (LR2000 code), later closed on expiry: still approved.
        (
            [_action("124", "2017-03-01"), _action("307", "2018-06-01"), _action("970", "2023-01-01")],
            Decision(date(2017, 3, 1), "approved", date(2018, 6, 1)),
        ),
        # Granted under MLRS's own code.
        (
            [_action("124", "2022-01-10", "Approved/Accepted"), _action("8307", "2024-12-02", "Approved/Accepted")],
            Decision(date(2022, 1, 10), "approved", date(2024, 12, 2)),
        ),
        # Geothermal lease issued.
        ([_action("124", "2019-02-01"), _action("237", "2020-07-01")], Decision(date(2019, 2, 1), "approved", date(2020, 7, 1))),
        ([_action("124", "2019-02-01"), _action("130", "2020-01-15")], Decision(date(2019, 2, 1), "withdrawn", date(2020, 1, 15))),
        # Closed without a reason.
        (
            [_action("124", "2018-04-01"), _action("967", "2021-09-30")],
            Decision(date(2018, 4, 1), "outcome_unclear", date(2021, 9, 30)),
        ),
        # Rejected, reinstated, then granted: later dispositions win.
        (
            [
                _action("124", "2016-05-01"),
                _action("125", "2017-01-01"),
                _action("388", "2017-06-01"),
                _action("307", "2018-02-01"),
            ],
            Decision(date(2016, 5, 1), "approved", date(2018, 2, 1)),
        ),
        # Closed, reopened, still open.
        ([_action("124", "2016-05-01"), _action("970", "2017-01-01"), _action("388", "2017-06-01")], Decision(date(2016, 5, 1))),
        # No 124: Case Established is the filing date.
        ([_action("387", "2020-03-03"), _action("307", "2021-03-03")], Decision(date(2020, 3, 3), "approved", date(2021, 3, 3))),
        # Easement issued.
        ([_action("124", "2018-01-01"), _action("345", "2019-01-01")], Decision(date(2018, 1, 1), "approved", date(2019, 1, 1))),
        # A renewal on its own serial: filed (314), renewed (308).
        ([_action("314", "2019-05-01"), _action("308", "2019-10-23")], Decision(date(2019, 5, 1), "approved", date(2019, 10, 23))),
        # A renewal never moves the original grant.
        ([_action("124", "1999-01-01"), _action("307", "2000-01-01"), _action("314", "2029-01-01"), _action("308", "2030-01-01")], Decision(date(1999, 1, 1), "approved", date(2000, 1, 1))),
        # Post-authorization withdrawal/denial codes don't touch the outcome.
        ([_action("124", "2016-05-01"), _action("307", "2017-01-01"), _action("037", "2019-01-01"), _action("039", "2020-01-01")], Decision(date(2016, 5, 1), "approved", date(2017, 1, 1))),
        # Nothing decided yet.
        ([_action("124", "2024-01-01"), _action("104", "2024-03-01")], Decision(date(2024, 1, 1))),
        ([], Decision()),
    ],
)
def test_decode_actions(actions, expected):
    assert decode_actions(actions) == expected


def test_decode_actions_respects_action_status():
    actions = [
        _action("124", "2023-01-05", "Received"),  # a filing counts whatever its status
        _action("8307", "2024-01-01", "Filed"),  # issuance not yet effective
        _action("970", "2024-02-01", "Draft"),
    ]
    assert decode_actions(actions) == Decision(date(2023, 1, 5))
    assert decode_actions([_action("125", "2024-05-05", "Rejected/Denied")]).outcome == "denied"
    assert decode_actions([_action("130", "2024-05-05", "Filed")]).outcome == "withdrawn"
    assert decode_actions([_action("130", "2024-05-05", "Requested")]).outcome == "withdrawn"


def test_rejection_in_progress_explains_a_closure():
    filed = _action("124", "2020-01-01")
    rejected = _action("125", "2021-03-01", "Under Review")
    closed = _action("970", "2021-04-01", "Approved/Accepted")
    # Still under review and the case is open: not decided yet.
    assert decode_actions([filed, rejected]).outcome is None
    # Then closed: it was a rejection, on the rejection's date.
    assert decode_actions([filed, rejected, closed]) == Decision(date(2020, 1, 1), "denied", date(2021, 3, 1))
    # A draft rejection doesn't explain anything.
    draft = _action("125", "2021-03-01", "Draft")
    assert decode_actions([filed, draft, closed]).outcome == "outcome_unclear"


def test_decode_actions_uses_whichever_date_the_action_carries():
    action = {"Code__c": "124", "Status__c": "Approved/Accepted", "Action_Date__c": "2023-10-13", "Date_Filed__c": None}
    assert decode_actions([action]).filed_date == date(2023, 10, 13)


@pytest.mark.parametrize(
    "disposition, payload, expected",
    [
        ("Authorized", None, Decision(None, "approved", date(2021, 5, 4))),
        ("Closed", None, Decision(None, "outcome_unclear", date(2021, 5, 4))),
        ("Pending", None, Decision(None, "pending", None)),
        ("Interim", None, Decision(None, "pending", None)),
        ("Closed", {"error": "MLRS related-list call failed"}, Decision(None, "outcome_unclear", date(2021, 5, 4))),
        # MLRS has the filing but no decision: the layer fills the outcome.
        ("Authorized", {"actions": [_action("124", "2019-01-01")]}, Decision(date(2019, 1, 1), "approved", date(2021, 5, 4))),
        # MLRS's known-direction decision beats the layer.
        ("Closed", {"actions": [_action("124", "2019-01-01"), _action("130", "2020-02-02")]}, Decision(date(2019, 1, 1), "withdrawn", date(2020, 2, 2))),
        # A closure in MLRS doesn't override "currently authorized".
        ("Authorized", {"actions": [_action("970", "2020-02-02")]}, Decision(None, "approved", date(2021, 5, 4))),
    ],
)
def test_resolve_outcome_falls_back_to_layer_disposition(disposition, payload, expected):
    assert resolve_outcome(_case(disposition=disposition), payload) == expected


def test_in_window():
    assert in_window(Decision(date(2010, 1, 1), "pending"), START)
    assert in_window(Decision(date(2010, 1, 1), "approved", date(2016, 1, 1)), START)
    assert not in_window(Decision(date(2010, 1, 1), "approved", date(2015, 12, 31)), START)
    assert not in_window(Decision(None, "approved", None), START)
    assert in_window(Decision(date(2017, 1, 1), "approved", None), START)


# --- MLRS client and cache -------------------------------------------------


def test_parse_aura_context_from_saved_case_page():
    fwuid, app_id = mlrs.parse_aura_context((FIXTURES / "mlrs_case_page.html").read_text())
    assert fwuid.startswith("ZjkyMTR4")
    assert app_id == "1919_p_UNHwnUfNHtXRD9ejXneg"


def test_parse_aura_context_without_one():
    with pytest.raises(mlrs.MlrsError):
        mlrs.parse_aura_context("<html></html>")


class FakeMlrsSession:
    """Serves the saved case page and Aura response; answers clientOutOfSync
    the first `out_of_sync` times."""

    def __init__(self, out_of_sync=0):
        self.headers = {}
        self.out_of_sync = out_of_sync
        self.gets = 0
        self.posts = []

    def get(self, url, timeout):
        self.gets += 1
        return FakeResponse((FIXTURES / "mlrs_case_page.html").read_text())

    def post(self, url, params, data, timeout):
        self.posts.append(data)
        if self.out_of_sync:
            self.out_of_sync -= 1
            return FakeResponse('*/{"event":{"descriptor":"markup://aura:clientOutOfSync"}}/*ERROR*/')
        body = json.loads((FIXTURES / "mlrs_related_list_records.json").read_text())
        actions = json.loads(data["message"])["actions"]
        answers = []
        for action in actions:
            answer = dict(body["actions"][0], id=action["id"])
            answers.append(answer)
        return FakeResponse(json.dumps({"actions": answers}))


def test_mlrs_client_fetches_actions_with_page_framework_id():
    session = FakeMlrsSession()
    client = mlrs.MlrsClient(session=session, min_interval=0)
    result = client.fetch_case("a023d00000GZjGqAAL")
    assert [record["Code__c"] for record in result["actions"]][:2] == ["970", "125"]
    sent = session.posts[0]
    context = json.loads(sent["aura.context"])
    assert context["fwuid"].startswith("ZjkyMTR4")
    params = json.loads(sent["message"])["actions"][0]["params"]
    assert params["relatedListId"] == "Case_Actions__r"
    assert "Case_Action__c.Code__c" in params["fields"]
    assert client.requests_made == 2  # page + one Aura call


def test_mlrs_client_rereads_framework_id_when_out_of_sync():
    session = FakeMlrsSession(out_of_sync=1)
    client = mlrs.MlrsClient(session=session, min_interval=0)
    result = client.fetch_case("a023d00000GZjGqAAL", with_customers=True)
    assert result["actions"] and "customers" in result
    assert session.gets == 2
    assert len(json.loads(session.posts[-1]["message"])["actions"]) == 2


class CountingClient:
    def __init__(self):
        self.calls = []

    def fetch_case(self, sf_id, with_customers=False):
        self.calls.append(sf_id)
        return {"actions": [_action("124", "2019-01-01"), _action("307", "2020-01-01")]}


def test_case_cache_makes_no_requests_when_nothing_changed(db_session):
    cases = [_case(), _case(cse_nr="NMNM106325500", sf_id="a023d00000HEtozAAD", disposition="Pending", cse_disp_dt=None)]
    client = CountingClient()
    log = lambda _: None
    assert refresh_case_cache(db_session, cases, client, log=log) == 2
    assert refresh_case_cache(db_session, cases, client, log=log) == 0
    assert len(client.calls) == 2

    # A disposition change in the layer triggers a refetch of that case only.
    cases[1].disposition, cases[1].cse_disp_dt = "Authorized", date(2025, 1, 1)
    assert refresh_case_cache(db_session, cases, client, log=log) == 1
    assert client.calls[-1] == "a023d00000HEtozAAD"
    entry = db_session.get(BlmCaseCache, "NMNM106325500")
    assert entry.cse_disp == "Authorized" and entry.payload["actions"]


def test_case_cache_stores_mlrs_errors_and_skips_network_failures(db_session):
    import requests

    class FailingClient:
        def fetch_case(self, sf_id, with_customers=False):
            if sf_id == "gone":
                raise mlrs.MlrsError("no such case")
            raise requests.ConnectionError("down")

    cases = [_case(cse_nr="A1", sf_id="gone"), _case(cse_nr="A2", sf_id="flaky")]
    assert refresh_case_cache(db_session, cases, FailingClient(), log=lambda _: None) == 1
    assert db_session.get(BlmCaseCache, "A1").payload == {"error": "no such case"}
    assert db_session.get(BlmCaseCache, "A2") is None


# --- Source 3: ePlanning ---------------------------------------------------


def test_parse_saved_eplanning_project_page():
    fields = eplanning.parse_project_page((FIXTURES / "eplanning_project_page.html").read_text())
    assert fields["NEPA Number"] == "DOI-BLM-AZ-C020-2022-0006-EIS"
    assert fields["Project Type"] == "EIS"
    assert fields["Case File Number"] == "AZA037939"
    assert fields["Program"] == "Renewable Energy"
    assert fields["Sub-Program"] == "Solar Energy"
    assert fields["Decision Date"] == "1/2/2025"
    assert not any(label.startswith("Contact") for label in fields)


def test_parse_saved_search_results_with_stray_backslash():
    text = (FIXTURES / "eplanning_search_results.json").read_text()
    with pytest.raises(ValueError):
        json.loads(text)
    rows = eplanning.parse_search_results(text)
    assert rows[0]["projectid"] == "8ec379d0-a7f2-f011-8407-001dd80bcf93"
    assert rows[0]["type"] == "EIS"
    assert rows[2]["projectname"].endswith("Permit\\CX")


@pytest.mark.parametrize(
    "text, keys",
    [
        ("AZA037939", {"AZA37939"}),
        ("AZA    037939", {"AZA37939"}),
        ("AZA-34187, AZA-36502", {"AZA34187", "AZA36502"}),
        ("AZA 03418702", {"AZA34187"}),  # segment 02 of AZA 034187
        ("CACA 053213-03", {"CACA53213"}),
        ("CACA-58539 07", {"CACA58539"}),
        ("CA-956A, CA-958, and CA-1862", {"CA956", "CA958", "CA1862"}),
        ("COC 038521A", {"COC38521"}),
        ("AZA_017786", {"AZA17786"}),
        ("CACA105882914", {"CACA105882914"}),  # MLRS serial: never split
        ("", set()),
        (None, set()),
    ],
)
def test_serial_keys(text, keys):
    assert eplanning.serial_keys(text) == keys


@pytest.mark.parametrize(
    "project_type, tier",
    [("CE", "cx"), ("DNA", "dna"), ("EA", "ea"), ("EIS", "eis"), ("RMP Amendment - EIS", "eis"), ("OTHER NEPA", None), ("", None)],
)
def test_review_tier(project_type, tier):
    assert eplanning.review_tier(project_type) == tier


def test_wanted_listing_narrows_fluid_minerals_only():
    assert eplanning.wanted_listing({"program": "Lands and Realty", "projectname": "Fiber optic line"})
    assert eplanning.wanted_listing({"program": "Fluid Minerals (Oil &amp; Gas, Tar Sands, Oil Shale)", "projectname": "Gathering Pipeline ROW"})
    assert not eplanning.wanted_listing({"program": "Fluid Minerals (Oil &amp; Gas, Tar Sands, Oil Shale)", "projectname": "Federal 12-3 APD"})


def test_tiers_by_serial_keeps_highest_tier():
    payloads = [
        {"Project Type": "CE", "Case File Number": "AZA-37939", "NEPA Number": "N-CX"},
        {"Project Type": "EIS", "Case File Number": "AZA037939", "NEPA Number": "N-EIS"},
        {"Project Type": "EA", "Case File Number": "AZA 037939", "NEPA Number": "N-EA"},
        {"Project Type": "OTHER NEPA", "Case File Number": "NVN-1"},
        {"Project Type": "EA"},
    ]
    tiers = eplanning.tiers_by_serial(payloads)
    assert tiers == {"AZA37939": {"review_tier": "eis", "nepa_number": "N-EIS", "project_id": None}}


def test_eplanning_cache_refetches_only_changed_projects(db_session):
    listing = json.loads(json.dumps(eplanning.parse_search_results((FIXTURES / "eplanning_search_results.json").read_text())))

    class FakeClient:
        def __init__(self):
            self.pages = []

        def list_projects(self, program_id):
            return listing if program_id == eplanning.PROGRAMS["Renewable Energy"] else []

        def fetch_project(self, project_id):
            self.pages.append(project_id)
            return {"Project Type": "EIS", "Case File Number": "AZA037939"}

    client = FakeClient()
    assert eplanning.refresh_cache(db_session, client, log=lambda _: None) == 3
    assert eplanning.refresh_cache(db_session, client, log=lambda _: None) == 0
    listing[0]["nepastatus"] = "In Progress - Scoping"
    assert eplanning.refresh_cache(db_session, client, log=lambda _: None) == 1
    entry = db_session.get(BlmEplanningCache, listing[0]["projectid"])
    assert entry.payload["_project_id"] == listing[0]["projectid"]
    assert entry.payload["Case File Number"] == "AZA037939"


# --- Join and upsert -------------------------------------------------------


def test_build_records_joins_review_tier_on_case_file_number():
    page = eplanning.parse_project_page((FIXTURES / "eplanning_project_page.html").read_text())
    tiers = eplanning.tiers_by_serial([page])
    joined = _case(cse_nr="AZAZ106000001", legacy_serial="AZA    037939", disposition="Pending", cse_disp_dt=None)
    by_mlrs_serial = _case(cse_nr="AZAZ106000002", legacy_serial=None, disposition="Pending", cse_disp_dt=None)
    tiers["AZAZ106000002"] = {"review_tier": "ea", "nepa_number": "x", "project_id": None}
    unlinked = _case(cse_nr="AZAZ106000003", legacy_serial="AZA 099999", disposition="Pending", cse_disp_dt=None)
    records = {r.case.cse_nr: r for r in build_records([joined, by_mlrs_serial, unlinked], {}, tiers, START)}
    assert records["AZAZ106000001"].review_tier == "eis" and records["AZAZ106000001"].is_major
    assert records["AZAZ106000002"].review_tier == "ea" and records["AZAZ106000002"].is_major
    assert records["AZAZ106000003"].review_tier is None and not records["AZAZ106000003"].is_major


def test_build_records_drops_cases_decided_before_window_and_names_applicant():
    old = _case(cse_nr="OLD1")
    payloads = {
        "OLD1": {"actions": [_action("124", "2001-01-01"), _action("307", "2002-01-01")]},
        "NEW1": {"actions": [_action("124", "2020-01-01")], "customers": [{"Account__r.Name": "GEO POWER INC"}]},
    }
    new = _case(cse_nr="NEW1", project_type="geothermal", action_type="geothermal_lease", disposition="Pending", customer=None)
    records = build_records([old, new], payloads, {}, START)
    assert [r.case.cse_nr for r in records] == ["NEW1"]
    assert records[0].applicant == "GEO POWER INC"


def test_upsert_records_is_idempotent_and_leaves_ferc_alone(db_session):
    ferc = Project(name="Some Pipeline", project_type="natural_gas_pipeline", applicant="Gas Co", region="PA")
    db_session.add(ferc)
    db_session.flush()
    db_session.add(AgencyAction(project_id=ferc.id, agency="FERC", docket_number="CP17-101-000", action_type="certificate"))
    db_session.flush()

    record = build_records(
        [_case()], {"AZAZ105846925": {"actions": [_action("124", "2019-03-01"), _action("125", "2021-05-04")]}}, {}, START
    )
    assert upsert_records(db_session, record) == 1
    assert upsert_records(db_session, record) == 1
    blm = db_session.query(AgencyAction).filter_by(agency="BLM").one()
    assert (blm.docket_number, blm.action_type, blm.outcome) == ("AZAZ105846925", "row_grant", "denied")
    assert (blm.filed_date, blm.decision_date) == (date(2019, 3, 1), date(2021, 5, 4))
    assert blm.source_url == "https://mlrs.blm.gov/s/blm-case/a023d00000Ga2HoAAJ/x"
    assert blm.review_tier is None and blm.is_major is False
    project = blm.project
    assert (project.name, project.applicant, project.region) == ("SONORAN SOLAR ENERGY LLC (AZAZ105846925)", "SONORAN SOLAR ENERGY LLC", "AZ")
    assert db_session.query(Project).count() == 2
    assert db_session.get(Project, ferc.id).name == "Some Pipeline"


def test_ferc_upsert_never_matches_a_blm_serial(db_session):
    from scripts.ingest_ferc import ActionRecord
    from scripts.ingest_ferc import upsert_records as upsert_ferc

    blm_project = Project(name="Odd BLM case", project_type="oil_gas_pipeline", applicant="X", region="WY")
    db_session.add(blm_project)
    db_session.flush()
    # Even a BLM serial that looks like a FERC docket isn't matched: the FERC
    # lookup is scoped to agency = "FERC".
    db_session.add(AgencyAction(project_id=blm_project.id, agency="BLM", docket_number="CP17-101-000", action_type="row_grant"))
    db_session.flush()
    upsert_ferc(db_session, [ActionRecord("CP17-101-000", "natural_gas_pipeline", "certificate", applicant="Gas Co")])
    ferc = db_session.query(AgencyAction).filter_by(agency="FERC").one()
    assert ferc.project_id != blm_project.id
    assert db_session.get(Project, blm_project.id).project_type == "oil_gas_pipeline"


def test_run_ingestion_end_to_end_without_network(db_session, monkeypatch):
    cases = [_case(), _case(cse_nr="NVNV105000009", sf_id="a02y", legacy_serial="NVN 091234", disposition="Pending", cse_disp_dt=None)]
    monkeypatch.setattr(ingest_blm, "fetch_cases", lambda session, start, types, log: cases)
    db_session.add(
        BlmEplanningCache(
            project_id="p1",
            fetched_at=__import__("datetime").datetime(2026, 11, 1),
            nepa_status="Completed",
            payload={"Project Type": "EA", "Case File Number": "NVN-91234"},
        )
    )
    client = CountingClient()
    total = ingest_blm.run_ingestion(db_session, START, session=object(), mlrs_client=client, log=lambda _: None)
    assert total == 2 and len(client.calls) == 2
    actions = {a.docket_number: a for a in db_session.query(AgencyAction).filter_by(agency="BLM")}
    assert actions["AZAZ105846925"].outcome == "approved"
    assert actions["NVNV105000009"].review_tier == "ea" and actions["NVNV105000009"].is_major
    # Re-run: cache is current, so no MLRS requests.
    ingest_blm.run_ingestion(db_session, START, session=object(), mlrs_client=client, log=lambda _: None)
    assert len(client.calls) == 2


def test_migrate_is_safe_on_sqlite(db_session):
    from scripts.blm_schema import migrate

    migrate(db_session.get_bind())
    migrate(db_session.get_bind())
