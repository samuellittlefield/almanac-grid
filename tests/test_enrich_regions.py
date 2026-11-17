from __future__ import annotations

import datetime
import io

import openpyxl
import pytest

from app.models import AgencyAction, Project
import requests

from scripts.enrich_regions import (
    apply_regions,
    fetch_ferc_dataset,
    find_ferc_hydro_file_url,
    parse_eia_pipeline_states,
    parse_ferc_dataset_states,
    parse_ferc_hydro_states,
)


def _workbook(sheets: dict[str, list[tuple]]) -> io.BytesIO:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def test_parse_eia_pipeline_states():
    header = ("Project Name", "State(s)", "Beg_State", "Authority", "Docket/Permit  Number")
    source = _workbook(
        {
            "Contents": [(None, "Workbook Contents")],
            "Natural Gas Pipeline Projects": [
                ("Natural Gas Pipeline Projects",),
                header,
                ("Expansion", "NV", "NV", "FERC", "CP25-209"),
                ("Pre-filing", "NV", "NV", "FERC", "PF26-5"),
                ("Two dockets", "NJ,NY, PA", "NJ", "FERC", "CP17-101; cp17-102"),
            ],
            "Historical Projects (1996-2024)": [
                ("Natural Gas Pipeline Projects",),
                ("Project Name", "State(s)", "Beg_State", "Authority", "Docket Number"),
                ("Offshore", "GM,LA", "GM", "FERC", "CP96-91"),
                ("Intrastate", "TX", "TX", "State", "na"),
            ],
        }
    )

    assert parse_eia_pipeline_states(source) == {
        "CP25-209": ["NV"],
        "CP17-101": ["NJ", "NY", "PA"],
        "CP17-102": ["NJ", "NY", "PA"],
        # "GM" is the Gulf of Mexico, not a state.
        "CP96-91": ["LA"],
    }


def test_parse_ferc_hydro_states():
    title = ("Pending Licenses, Relicenses and Exemptions",)
    pending = _workbook(
        {"PLRE": [title, ("FERC: eLibrary",), ("Project Number", "Project Name", "Licensee", "State(s)"),
                  (2639, "Cornell", "Northern States Power Co", "WI"),
                  ("P-1234", "Two States", "X", "OR, WA")]}
    )
    issued = _workbook(
        {"LE": [title, ("FERC: eLibrary",), ("Project Number", "Project Name", "Waterway", "State"),
                (400, "Ames", "San Miguel River", "CO")]}
    )

    assert parse_ferc_hydro_states(pending) == {"P-2639": ["WI"], "P-1234": ["OR", "WA"]}
    assert parse_ferc_hydro_states(issued) == {"P-400": ["CO"]}


def test_parse_ferc_dataset_states():
    body = {
        "row_data": [
            {"Project_Number": "P-5", "Project_Name": "Séliš Ksanka Qíispé", "State(s)": "MT"},
            {"Project_Number": "P-1893", "Project_Name": "Merrimack River", "State(s)": "MA, NH, VT"},
            {"Project_Number": "P-77", "State(s)": None},
        ],
        "row_count": 3,
        "has_more": False,
    }

    assert parse_ferc_dataset_states(body) == {"P-5": ["MT"], "P-1893": ["MA", "NH", "VT"]}


def test_parse_ferc_dataset_states_refuses_a_partial_page():
    with pytest.raises(LookupError):
        parse_ferc_dataset_states({"row_data": [], "has_more": True})


def test_fetch_ferc_dataset_masks_the_key_in_errors():
    class Session:
        def get(self, url, params, **kwargs):
            raise requests.HTTPError(f"403 Client Error: Forbidden for url: {url}?api_key={params['api_key']}")

    with pytest.raises(LookupError) as raised:
        fetch_ferc_dataset(Session(), 31, "secret-key-123")

    assert "secret-key-123" not in str(raised.value)
    assert "<FERC_API_KEY>" in str(raised.value)


def _project(db, project_type, region, dockets):
    project = Project(name=dockets[0], project_type=project_type, applicant="X", region=region)
    db.add(project)
    db.flush()
    for docket in dockets:
        db.add(AgencyAction(project_id=project.id, docket_number=docket, action_type="application"))
    db.flush()
    return project


def test_apply_regions(db_session):
    multi = _project(db_session, "natural_gas_pipeline", None, ["CP17-101-000", "CP17-101-007"])
    guessed = _project(db_session, "natural_gas_pipeline", "TX", ["CP19-5-000"])
    hydro = _project(db_session, "hydroelectric", None, ["P-2639-028"])
    unlisted = _project(db_session, "natural_gas_pipeline", "PA", ["CP20-9-000"])

    updated = apply_regions(
        db_session, {"CP17-101": ["NJ", "NY", "PA"], "CP19-5": ["LA"], "P-2639": ["WI"]}
    )

    assert updated == 3
    assert (multi.region, guessed.region, hydro.region, unlisted.region) == ("NJ,NY,PA", "LA", "WI", "PA")


class _FakeSession:
    def __init__(self, live_urls):
        self.live_urls = set(live_urls)
        self.requested: list[str] = []

    def head(self, url, **kwargs):
        self.requested.append(url)

        class Response:
            status_code = 200 if url in self.live_urls else 404

        return Response()


def test_find_ferc_hydro_file_url_walks_back_to_latest():
    latest = "https://www.ferc.gov/sites/default/files/2026-11/PLRE_11.9.2026.xlsx"
    session = _FakeSession([latest, "https://www.ferc.gov/sites/default/files/2026-10/PLRE_10.6.2026.xlsx"])

    assert find_ferc_hydro_file_url(session, "PLRE", datetime.date(2026, 11, 17)) == latest


def test_find_ferc_hydro_file_url_zero_padded_name():
    url = "https://www.ferc.gov/sites/default/files/2021-01/LE_Less2Years_01.15.2021.xlsx"

    assert find_ferc_hydro_file_url(_FakeSession([url]), "LE_Less2Years", datetime.date(2021, 1, 20)) == url


def test_find_ferc_hydro_file_url_none_published():
    with pytest.raises(LookupError):
        find_ferc_hydro_file_url(_FakeSession([]), "PLRE", datetime.date(2026, 11, 17))


@pytest.fixture()
def regions(db_session):
    _project(db_session, "natural_gas_pipeline", "NY,PA", ["CP17-101-000"])
    _project(db_session, "natural_gas_pipeline", "PA", ["CP18-1-000"])
    _project(db_session, "hydroelectric", "WA", ["P-1-001"])
    db_session.commit()


@pytest.mark.parametrize(
    "region,expected",
    [("pa", ["CP17-101-000", "CP18-1-000"]), ("NY", ["CP17-101-000"]), ("P", []), ("%", [])],
)
def test_region_filter_matches_any_listed_state(client, regions, region, expected):
    body = client.get("/projects", params={"region": region}).json()
    assert [item["name"] for item in body["items"]] == expected


def test_region_options_are_single_states(client, regions):
    page = client.get("/ui/projects").text
    for code in ("NY", "PA", "WA"):
        assert f'<option value="{code}"' in page
    assert 'value="NY,PA"' not in page
