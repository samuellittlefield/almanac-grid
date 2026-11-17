"""BLM ePlanning (National NEPA Register, https://eplanning.blm.gov): the NEPA
review tier (CX / DNA / EA / EIS) behind BLM cases.

The site is a Power Pages portal. Its project search page fills its table from
`POST /searchresults/` (form-encoded, DataTables-style); with `download=true`
the same call returns every matching project in one response. That listing
has NEPA number, project type, status and program, but not the case file
number, so each candidate project's server-rendered page
(`/Project-Home/?id=<guid>`) is fetched once and its "Case File Number" field
parsed. Pages are cached in `blm_eplanning_cache` and refetched only when the
listing shows a different NEPA status.
"""

from __future__ import annotations

import datetime
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

import requests
from sqlalchemy.orm import Session

from app.models import BlmEplanningCache

BASE_URL = "https://eplanning.blm.gov"
SEARCH_PAGE_URL = BASE_URL + "/Search/"
SEARCH_RESULTS_URL = BASE_URL + "/searchresults/"
PROJECT_URL = BASE_URL + "/Project-Home/?id={project_id}"
USER_AGENT = "almanac-grid-ingest/2.0"
MIN_INTERVAL_SECONDS = 0.5
# Pages take one to two seconds to serve, so four in flight keep close to the
# 2 requests/s limit without exceeding it.
PAGE_WORKERS = 4

# Program filter ids on the search page (option values of #programDropdown).
PROGRAMS = {
    "Renewable Energy": "8be74528-2bea-f011-8544-001dd80f931d",
    "Lands and Realty": "9be74528-2bea-f011-8544-001dd80f931d",
    "Fluid Minerals": "91e74528-2bea-f011-8544-001dd80f931d",
}
# Fluid Minerals is ~11k projects, nearly all oil & gas drilling permits whose
# case file is a lease, not a right-of-way. Only fetch the ones whose name
# says they're about a geothermal lease or a line on a right-of-way.
_FLUID_MINERALS_NAME_RE = re.compile(r"geotherm|pipe ?line|right[- ]of[- ]way|\bROW\b|gathering|transmission", re.I)

_TIERS = {
    "CE": "cx",
    "CX": "cx",
    "DNA": "dna",
    "EA": "ea",
    "EIS": "eis",
    "RMP Amendment - EA": "ea",
    "RMP Amendment - EIS": "eis",
}
TIER_RANK = {"cx": 1, "dna": 2, "ea": 3, "eis": 4}

_FIELD_RE = re.compile(
    r'<strong>\s*(?P<label>[^<]+?)\s*</strong>\s*</div>\s*'
    r'<div class="(?:preview-info-data|date-texts)">(?P<value>.*?)</div>',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
# A case serial inside free text: letters, then the number, then an optional
# segment suffix either attached ("CA-956A") or after a separator
# ("CACA 053213-03", "CACA-58539 07"). Covers legacy LR2000 serials
# ("AZA 037939", "NVN-91234") and MLRS serials ("NVNV105123456").
_SERIAL_RE = re.compile(r"\b([A-Z]{1,6})[\s-]*(\d{1,9})(?:[A-Z]{1,2}\b|[\s-]\d{2}\b)?")


def serial_keys(text: str | None) -> set[str]:
    """Join keys for every case serial in `text`: the letters plus the number
    without zero padding, so 'AZA 034187', 'AZA-34187' and 'aza034187' all
    give 'AZA34187'. Segment suffixes are dropped, so the segments of one
    case ('AZA 03418702' is segment 02 of AZA 034187) join to the same
    ePlanning project. A zero-led number of 8+ digits is a legacy serial with
    its 2-digit segment run in."""
    keys: set[str] = set()
    for letters, digits in _SERIAL_RE.findall((text or "").upper()):
        if len(digits) >= 8 and digits.startswith("0"):
            digits = digits[:-2]
        keys.add(letters + (digits.lstrip("0") or "0"))
    return keys


def review_tier(project_type: str | None) -> str | None:
    return _TIERS.get((project_type or "").strip())


def parse_project_page(page_html: str) -> dict:
    """Labelled fields of a Project-Home page: {"NEPA Number": ..., "Project
    Type": ..., "Case File Number": ..., "Program": ...}. Repeated labels keep
    their first value. BLM staff contact details are left out."""
    fields: dict[str, str] = {}
    for match in _FIELD_RE.finditer(page_html):
        label = html.unescape(match.group("label")).strip()
        value = html.unescape(_TAG_RE.sub(" ", match.group("value")))
        value = re.sub(r"\s+", " ", value).strip()
        if label.startswith("Contact"):
            continue
        if label and value and label not in fields:
            fields[label] = value
    return fields


def parse_search_results(text: str) -> list[dict]:
    """The /searchresults/ body is JSON padded with whitespace; project names
    can contain stray backslashes that aren't valid JSON escapes."""
    cleaned = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text.strip())
    return json.loads(cleaned)["data"]


def wanted_listing(row: dict) -> bool:
    program = html.unescape(row.get("program") or "")
    if program.startswith("Fluid Minerals"):
        return bool(_FLUID_MINERALS_NAME_RE.search(html.unescape(row.get("projectname") or "")))
    return True


class EplanningClient:
    def __init__(self, session: requests.Session | None = None, min_interval: float = MIN_INTERVAL_SECONDS):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.min_interval = min_interval
        self.requests_made = 0
        self._last_request = 0.0
        self._lock = threading.Lock()
        self._primed = False

    def _get(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(4):
            with self._lock:  # shared by the page workers
                wait = self._last_request + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_request = time.monotonic()
                self.requests_made += 1
            try:
                response = self.session.request(method, url, timeout=120, **kwargs)
                if response.status_code < 500:
                    response.raise_for_status()
                    return response
            except requests.RequestException:
                if attempt == 3:
                    raise
            time.sleep(5 * (attempt + 1))
        response.raise_for_status()
        return response

    def list_projects(self, program_id: str) -> list[dict]:
        if not self._primed:
            self._get(SEARCH_PAGE_URL)  # session cookies the search call expects
            self._primed = True
        data = {
            "order_attribute": "0",
            "order_direction": "asc",
            "program": program_id,
            "active": "false",
            "download": "true",
            "filter_total_count": "0",
            "get_total_count": "false",
            "start": "0",
            "page": "1",
        }
        response = self._request("POST", SEARCH_RESULTS_URL, data=data, headers={"Accept": "application/json"})
        return parse_search_results(response.text)

    def fetch_project(self, project_id: str) -> dict:
        return parse_project_page(self._get(PROJECT_URL.format(project_id=project_id)).text)


def refresh_cache(db: Session, client: EplanningClient, log=print) -> int:
    """Fetch every candidate project page that isn't cached or whose NEPA
    status changed. Commits as it goes, so an interrupted run resumes.
    Returns pages fetched."""
    listings: dict[str, dict] = {}
    for program, program_id in PROGRAMS.items():
        rows = [row for row in client.list_projects(program_id) if wanted_listing(row)]
        log(f"  ePlanning {program}: {len(rows)} projects")
        for row in rows:
            listings[row["projectid"]] = row

    cached = {entry.project_id: entry for entry in db.query(BlmEplanningCache)}
    todo = [
        row
        for project_id, row in listings.items()
        if project_id not in cached or cached[project_id].nepa_status != html.unescape(row.get("nepastatus") or "")
    ]
    log(f"  ePlanning: {len(todo)} project pages to fetch ({len(listings) - len(todo)} cached)")
    fetched = 0

    def fetch(row):
        try:
            return row, client.fetch_project(row["projectid"])
        except requests.RequestException as error:
            log(f"  ePlanning {row['projectid']}: skipped ({error})")
            return row, None

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
        results = pool.map(fetch, todo)
        for row, fields in results:
            if fields is None:
                continue
            fetched += _store(db, cached, row, fields)
            if fetched % 100 == 0:
                db.commit()
                log(f"  ePlanning: {fetched}/{len(todo)} pages")
    db.commit()
    return fetched


def _store(db: Session, cached: dict, row: dict, fields: dict) -> int:
    project_id = row["projectid"]
    fields["_project_id"] = project_id
    fields["_listing_type"] = html.unescape(row.get("type") or "")
    fields["_listing_program"] = html.unescape(row.get("program") or "")
    entry = cached.get(project_id) or BlmEplanningCache(project_id=project_id)
    entry.fetched_at = datetime.datetime.utcnow()
    entry.nepa_status = html.unescape(row.get("nepastatus") or "")
    entry.payload = fields
    cached[project_id] = db.merge(entry)
    return 1


def tiers_by_serial(payloads: Iterable[dict]) -> dict[str, dict]:
    """{serial key: {"review_tier", "nepa_number", "project_id"}}
    from cached project pages. A case named by several ePlanning projects
    (often a later maintenance CX on top of the original EA) gets the
    highest tier."""
    found: dict[str, dict] = {}
    for payload in payloads:
        tier = review_tier(payload.get("Project Type") or payload.get("_listing_type"))
        if tier is None:
            continue
        for serial in serial_keys(payload.get("Case File Number")):
            current = found.get(serial)
            if current is None or TIER_RANK[tier] > TIER_RANK[current["review_tier"]]:
                found[serial] = {
                    "review_tier": tier,
                    "nepa_number": payload.get("NEPA Number"),
                    "project_id": payload.get("_project_id"),
                }
    return found
