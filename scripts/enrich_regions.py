"""Fill `project.region` from published project lists keyed by docket number.

FERC's eLibrary search results carry no location, so ingestion only gets a
state when a filing description names one. These public spreadsheets list the
states for each project and are matched on docket / project number:

- EIA's Natural Gas Pipeline Projects tracker: FERC CP docket + states, for
  capacity-adding pipeline projects since 1996. Smaller certificate work
  (replacements, compressor changes) isn't in it.
- FERC's hydro lists: pending license / relicense / exemption applications
  (PLRE), and licenses and exemptions that took under two years to issue,
  cumulative since 2007 (LE_Less2Years).
- FERC's Active Licenses and Active Exemptions datasets on data.ferc.gov,
  which cover long relicensings the lists above miss. These need an API key
  (`FERC_API_KEY` in `.env`) and are skipped without one.

EL complaints have no project location, so they are left alone.

`region` holds comma-separated state codes ("NY,CT"), since pipelines cross
state lines. These lists are authoritative, so they replace a state that was
read out of a filing description.

Runs at the end of `python -m scripts.ingest_ferc`, or on its own:
    python -m scripts.enrich_regions
"""

from __future__ import annotations

import io
import re
from datetime import date, timedelta
from typing import BinaryIO, Iterable
from urllib.parse import urljoin

import openpyxl
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AgencyAction, Project
from scripts.ingest_ferc import _STATE_ABBREVIATIONS, _build_http_session, base_docket_number

EIA_DATA_PAGE = "https://www.eia.gov/naturalgas/data.php"
FERC_FILES_URL = "https://www.ferc.gov/sites/default/files/{folder}/{name}"
FERC_HYDRO_FILES = ("PLRE", "LE_Less2Years")
FERC_API_DATA_URL = "https://api.data.ferc.gov/v1/dataset/{id}/data"
FERC_HYDRO_DATASETS = (("active licenses", 31), ("active exemptions", 30))
# FERC republishes these roughly monthly under a dated filename.
FERC_LOOKBACK_DAYS = 120

STATE_CODES = set(_STATE_ABBREVIATIONS.values()) | {"DC"}
_CP_DOCKET_RE = re.compile(r"\bCP\d{2}-\d+\b", re.I)


def _states(value) -> list[str]:
    codes = [code.upper() for code in re.split(r"[\s,;/]+", str(value or "")) if code]
    return [code for code in dict.fromkeys(codes) if code in STATE_CODES]


def _header_rows(source: BinaryIO, required: Iterable[str]):
    """Yield (header, row) for every data row of every sheet whose header row
    (within the first 15 rows) has a column matching each `required` pattern."""
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    for sheet in workbook.worksheets:
        rows = sheet.iter_rows(values_only=True)
        header = None
        for _ in range(15):
            row = next(rows, None)
            if row is None:
                break
            cells = [str(cell or "").strip() for cell in row]
            if all(any(re.search(pattern, cell, re.I) for cell in cells) for pattern in required):
                header = cells
                break
        if header is None:
            continue
        for row in rows:
            yield header, row


def _column(header: list[str], pattern: str) -> int:
    return next(i for i, cell in enumerate(header) if re.search(pattern, cell, re.I))


def _merge(found: dict[str, list[str]], key: str, states: list[str]) -> None:
    if states:
        found[key] = list(dict.fromkeys(found.get(key, []) + states))


def parse_eia_pipeline_states(source: BinaryIO) -> dict[str, list[str]]:
    """{"CP17-101": ["NJ", "NY", "PA"], ...} from EIA's pipeline projects workbook."""
    found: dict[str, list[str]] = {}
    for header, row in _header_rows(source, (r"^docket", r"^state\(s\)$")):
        docket_cell = row[_column(header, r"^docket")]
        states = _states(row[_column(header, r"^state\(s\)$")])
        for docket in _CP_DOCKET_RE.findall(str(docket_cell or "")):
            _merge(found, docket.upper(), states)
    return found


def parse_ferc_hydro_states(source: BinaryIO) -> dict[str, list[str]]:
    """{"P-2639": ["WI"], ...} from a FERC hydro project list."""
    found: dict[str, list[str]] = {}
    for header, row in _header_rows(source, (r"^project number$", r"^state")):
        number = re.search(r"\d+", str(row[_column(header, r"^project number$")] or ""))
        if number:
            _merge(found, f"P-{int(number.group())}", _states(row[_column(header, r"^state")]))
    return found


def parse_ferc_dataset_states(body: dict) -> dict[str, list[str]]:
    """{"P-2639": ["WI"], ...} from a data.ferc.gov dataset response."""
    if body.get("has_more"):
        raise LookupError("response is paginated; only the first page would be read")
    found: dict[str, list[str]] = {}
    for row in body["row_data"]:
        number = re.search(r"\d+", str(row.get("Project_Number") or ""))
        if number:
            _merge(found, f"P-{int(number.group())}", _states(row.get("State(s)")))
    return found


def fetch_ferc_dataset(session: requests.Session, dataset_id: int, api_key: str) -> dict:
    """The key goes in the query string, so it's masked out of any error."""
    try:
        response = session.get(
            FERC_API_DATA_URL.format(id=dataset_id), params={"api_key": api_key}, timeout=120
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise LookupError(str(error).replace(api_key, "<FERC_API_KEY>")) from None


def find_eia_tracker_url(session: requests.Session) -> str:
    response = session.get(EIA_DATA_PAGE, timeout=60)
    response.raise_for_status()
    match = re.search(r'href="([^"]*EIA-NaturalGasPipelineProjects[^"]*\.xlsx)"', response.text)
    if not match:
        raise LookupError(f"no pipeline projects workbook linked from {EIA_DATA_PAGE}")
    return urljoin(EIA_DATA_PAGE, match.group(1))


def find_ferc_hydro_file_url(session: requests.Session, prefix: str, today: date) -> str:
    """FERC's licensing page (which links the current file) is behind a bot
    challenge, but the files themselves are not. Their names carry the
    publication date ("PLRE_11.9.2026.xlsx"), so walk back day by day."""
    for days_back in range(FERC_LOOKBACK_DAYS):
        day = today - timedelta(days=days_back)
        names = [f"{prefix}_{day.month}.{day.day}.{day.year}.xlsx"]
        padded = f"{prefix}_{day:%m.%d.%Y}.xlsx"
        if padded not in names:
            names.append(padded)
        for name in names:
            url = FERC_FILES_URL.format(folder=f"{day:%Y-%m}", name=name)
            if session.head(url, timeout=30, allow_redirects=True).status_code == 200:
                return url
    raise LookupError(f"no {prefix} file published in the last {FERC_LOOKBACK_DAYS} days")


def _download(session: requests.Session, url: str) -> BinaryIO:
    response = session.get(url, timeout=120)
    response.raise_for_status()
    return io.BytesIO(response.content)


def apply_regions(db: Session, states_by_docket: dict[str, list[str]]) -> int:
    """Set region on every project that has an action under a listed docket.
    Returns how many projects got a region."""
    dockets_by_project: dict[int, list[str]] = {}
    for project_id, docket in db.execute(select(AgencyAction.project_id, AgencyAction.docket_number)):
        dockets_by_project.setdefault(project_id, []).append(base_docket_number(docket))

    updated = 0
    for project_id, dockets in dockets_by_project.items():
        states: list[str] = []
        for docket in dict.fromkeys(dockets):
            states += states_by_docket.get(docket, [])
        if states:
            db.get(Project, project_id).region = ",".join(dict.fromkeys(states))
            updated += 1
    db.flush()
    return updated


def collect_states(session: requests.Session, today: date, api_key: str | None) -> dict[str, list[str]]:
    """Every source that can be reached; a missing one is reported and skipped."""

    def spreadsheet(locate, parse):
        def load():
            url = locate()
            return parse(_download(session, url)), url
        return load

    sources = [
        ("EIA pipeline projects", spreadsheet(lambda: find_eia_tracker_url(session), parse_eia_pipeline_states))
    ]
    for prefix in FERC_HYDRO_FILES:
        locate = lambda prefix=prefix: find_ferc_hydro_file_url(session, prefix, today)
        sources.append((f"FERC {prefix}", spreadsheet(locate, parse_ferc_hydro_states)))
    for name, dataset_id in FERC_HYDRO_DATASETS:
        def load(dataset_id=dataset_id):
            if not api_key:
                raise LookupError("no FERC_API_KEY in .env")
            body = fetch_ferc_dataset(session, dataset_id, api_key)
            return parse_ferc_dataset_states(body), f"data.ferc.gov dataset {dataset_id}"
        sources.append((f"FERC {name}", load))

    found: dict[str, list[str]] = {}
    for label, load in sources:
        try:
            parsed, origin = load()
        except (requests.RequestException, LookupError) as error:
            print(f"  {label}: skipped ({error})", flush=True)
            continue
        print(f"  {label}: {len(parsed)} dockets from {origin}", flush=True)
        for docket, states in parsed.items():
            _merge(found, docket, states)
    return found


def enrich_regions(
    db: Session,
    session: requests.Session | None = None,
    today: date | None = None,
    api_key: str | None = settings.ferc_api_key,
) -> int:
    session = session or _build_http_session()
    print("Filling project regions...", flush=True)
    updated = apply_regions(db, collect_states(session, today or date.today(), api_key))
    db.commit()
    return updated


def main() -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        updated = enrich_regions(db)
    finally:
        db.close()
    print(f"Set region on {updated} projects.")


if __name__ == "__main__":
    main()
