"""Ingest FERC eLibrary docket data into the `project` / `agency_action` tables.

FERC has no official bulk API, but the eLibrary docket-search UI
(https://elibrary.ferc.gov/eLibrary/search) is backed by a JSON endpoint
(`POST /eLibraryWebAPI/api/Search/AdvancedSearch`). This script drives that
endpoint with the same filters the UI offers (date range, docket prefix,
document class) instead of scraping rendered HTML.

How dockets map onto the data model:

- A base docket ("CP17-101", "P-2639") is one `project`.
- Each sub-docket with its own application ("CP17-101-000" the original
  certificate, "CP17-101-007" a later petition) is one `agency_action`. FERC
  issues the deciding order under the same sub-docket as the application, which
  is how filings and decisions are paired.

Run manually or via cron. Safe to re-run: rows are upserted, keyed on
(agency, docket_number, action_type).

Usage:
    python -m scripts.ingest_ferc
    python -m scripts.ingest_ferc --start-date 2020-01-01 --end-date 2024-01-01
    python -m scripts.ingest_ferc --docket-types CP P
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Iterator

import requests
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import AgencyAction, Project

ADVANCED_SEARCH_URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch"
USER_AGENT = "almanac-grid-ingest/1.0"

DEFAULT_PAGE_SIZE = 500
DEFAULT_SLEEP_SECONDS = 0.5
BACKFILL_YEARS = 10

ORDER_CLASS = "Order/Opinion"
APPLICATION_CLASS = "Application/Petition/Request"
WITHDRAWAL_CLASS = ("Applicant Correspondence", "Withdrawal of Application")


@dataclass(frozen=True)
class DecisionQuery:
    document_type: str
    # Description keyword; "*" fetches every order of this type. Used to keep
    # high-volume order types (hydro has ~28k delegated orders, nearly all
    # routine compliance letters) down to the ones that can be decisions.
    search_text: str = "*"


@dataclass(frozen=True)
class DocketPrefixConfig:
    """How to query and classify one FERC docket-number prefix."""

    prefix: str
    # "CP25-12" is year-coded by federal fiscal year; hydro project numbers
    # ("P-2639") are permanent and reused across decades of relicensing.
    year_coded: bool
    project_type: str
    # (document type within APPLICATION_CLASS, action_type it starts)
    application_types: tuple[tuple[str, str], ...]
    decision_queries: tuple[DecisionQuery, ...]
    # action_type for a later request on a year-coded docket (sub-docket != 000)
    amendment_action_type: str | None
    # How far before the window to look for the application behind a decision
    # that falls inside it.
    lookback_years: int


CP_CONFIG = DocketPrefixConfig(
    prefix="CP",
    year_coded=True,
    # NGA §7 pipeline certificates and NGA §3 LNG terminal authorizations share
    # the CP prefix; LNG is split back out by keyword in classify_project_type.
    project_type="natural_gas_pipeline",
    application_types=(("Certificate of Public Convenience and Necessity", "certificate_application"),),
    decision_queries=(DecisionQuery("Commission Order/Opinion"), DecisionQuery("Delegated Order")),
    amendment_action_type="certificate_amendment",
    lookback_years=1,
)

P_CONFIG = DocketPrefixConfig(
    prefix="P",
    year_coded=False,
    project_type="hydroelectric",
    application_types=(("License/Relicense Application", "license_application"),),
    decision_queries=(
        DecisionQuery("Commission Order/Opinion"),
        DecisionQuery("Delegated Order", '"Order Issuing"'),
        DecisionQuery("Delegated Order", '"Exemption from Licensing"'),
        DecisionQuery("Delegated Order", '"Order Denying"'),
        DecisionQuery("Delegated Order", '"Order Dismissing"'),
    ),
    amendment_action_type=None,
    # Relicensing routinely runs past five years, so the application behind an
    # in-window license order is often well before the window.
    lookback_years=10,
)

# EL is FERC's general electric docket: complaints and petitions for declaratory
# order, of which interconnection and transmission disputes are only a part.
# The filing descriptions ("Complaint of X under EL21-73") don't say what a
# docket is about, so it can't be narrowed to siting/interconnection from search
# results. Every original EL complaint or petition is ingested, with an
# action_type that says what it is.
EL_CONFIG = DocketPrefixConfig(
    prefix="EL",
    year_coded=True,
    project_type="electric_complaint",
    application_types=(
        ("Complaints", "complaint"),
        ("Petition for Declaratory Order", "declaratory_order_petition"),
    ),
    decision_queries=(DecisionQuery("Commission Order/Opinion"), DecisionQuery("Delegated Order")),
    # Later EL sub-dockets are compliance and rehearing, not new requests.
    amendment_action_type=None,
    lookback_years=1,
)

# The backstop transmission-siting authority in S.5653 (FPA §216/§224) is not
# law, so FERC has no docket prefix for it and there is nothing to ingest. When
# one exists, add its own DocketPrefixConfig here with this action type, so
# post-bill siting dockets stay separate from ordinary EL dockets instead of
# being folded into EL_CONFIG.
BACKSTOP_SITING_ACTION_TYPE = "backstop_siting_petition"

DOCKET_PREFIX_CONFIGS: list[DocketPrefixConfig] = [CP_CONFIG, P_CONFIG, EL_CONFIG]

_LNG_RE = re.compile(r"\bLNG\b|liquef|export terminal|import terminal", re.I)
_EL_NOT_APPLICATION_RE = re.compile(r"errata|supplement|tariff filing|fee collection|answer", re.I)
_EL_PROCEDURAL_RE = re.compile(r"rehearing|clarification|\bstay\b|compliance", re.I)
_DRAFT_RE = re.compile(r"\bdraft\b|pre-?application|notice of intent", re.I)
_REQUEST_RE = re.compile(r"\bappl\w*|\bpetition|\bamend|\bcomplaint|\brequest", re.I)
# Filed under the certificate document type but not decided by order: blanket
# certificate prior-notice activity takes effect unless protested, and the rest
# are housekeeping on an existing certificate.
_GAS_NOT_APPLICATION_RE = re.compile(
    r"prior[- ]notice|advance noti|blanket certificate|extension of time|"
    r"notice to proceed|in-?service|\breport\b|form no|supplement|waiver",
    re.I,
)
_ABANDONMENT_RE = re.compile(r"\babandon", re.I)
# The hydro license-application document type is also used for license
# transfers, amendments and compliance filings under license articles.
_HYDRO_APPLICATION_RE = re.compile(
    r"licen[sc]e application|appl\w*\.? for (?:(?:a|an|new|original|subsequent|minor|major)\s+)*licen[sc]e|"
    r"relicens|exemption",
    re.I,
)
_HYDRO_NOT_APPLICATION_RE = re.compile(
    r"transfer|amend|\barticle \d|as-built|surrender|extension|preliminary permit|pursuant to|"
    r"supplement|additional information|notice of|proof of",
    re.I,
)
_BUILD_RE = re.compile(r"certificat|construct|section 3|authori[sz]ation to (?:site|construct)", re.I)

# Only Commission/Director orders decide applications. Errata, notices and
# "Letter order ..." compliance letters share the same document types.
_IS_ORDER_RE = re.compile(
    r"^\s*(?:(?:Opinion No\.[^;]*;\s*)?Order(?:ing)?\b|Letter order (?:granting|approving)\b)", re.I
)

_MODIFIER = r"(?:(?:a|an|the|new|original|subsequent|minor|major|limited|blanket|in part)\s+){0,3}"
_APPROVAL_RES = {
    "gas": re.compile(
        rf"\b(?:re)?(?:issuing|amending|granting|approving|authorizing)\s+{_MODIFIER}"
        r"(?:certificat|authori[sz]ation|section 3|abandonment|construction|capacity (?:lease|increase)|"
        r"service area determination|amendment|application|petition)"
        # Uncontested applications are sometimes granted by a staff letter order.
        r"|^\s*Letter order (?:granting|approving)\b.*\bapplication\b",
        re.I,
    ),
    "hydroelectric": re.compile(
        rf"\b(?:issuing|granting)\s+{_MODIFIER}(?:licen[sc]e\b|exemption from licens)", re.I
    ),
    # Includes "Granting in Part and Denying in Part": the filer got relief.
    "electric_complaint": re.compile(
        r"\bgranting\b[^.]{0,60}?\b(?:complaint|petition|declaratory)", re.I
    ),
}
_DENIAL_RES = {
    "gas": re.compile(
        rf"\b(?:denying|rejecting|dismissing)\s+{_MODIFIER}"
        r"(?:certificat|application|authori[sz]ation|petition|amendment|request for (?:certificat|authori))",
        re.I,
    ),
    "hydroelectric": re.compile(
        rf"\b(?:denying|rejecting|dismissing)\s+{_MODIFIER}"
        r"(?:(?:re)?licen[sc]e application|application for (?:\w+\s+){0,2}licen[sc]e|"
        r"relicens\w+|exemption application|application for exemption)",
        re.I,
    ),
    "electric_complaint": re.compile(
        r"\b(?:denying|rejecting|dismissing)\b[^.]{0,60}?\b(?:complaint|petition|declaratory)", re.I
    ),
}

_STATE_ABBREVIATIONS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "west virginia": "WV", "virginia": "VA", "washington": "WA",
    "wisconsin": "WI", "wyoming": "WY",
}
# Longest names first so "West Virginia" isn't read as "Virginia".
# Only where the state is given as a place ("in Greene County, Pennsylvania").
# A bare state name is usually part of a company name ("Texas Eastern
# Transmission"), which says nothing about where the project is.
_STATE_RE = re.compile(
    r"(?:\b(?:County|Counties|Parish|Parishes|Borough|Township),|\b(?:in|near|offshore))\s+("
    + "|".join(re.escape(name) for name in sorted(_STATE_ABBREVIATIONS, key=len, reverse=True))
    + r")\b(?!\s+(?:Gas|Power|Electric|Public|Energy|Pipeline|Transmission|Department|State))",
    re.I,
)

_NAME = r"[A-Z][\w&.,/()\-’' ]*?"
_RE_RE_POSSESSIVE = re.compile(rf"\bre\s+(?:the\s+)?(?P<applicant>{_NAME})[’']s?\s+[A-Z0-9]")
_RE_RE_UNDER = re.compile(rf"\bre\s+(?:the\s+)?(?P<applicant>{_NAME})\s+under\b")
_RE_SUBMITS = re.compile(rf"^(?P<applicant>{_NAME})\s+submits?\b")
_RE_OF_APPLICANT = re.compile(
    rf"\b(?:Application|Petition|Request|Complaint)\b[^.]*?\bof\s+(?P<applicant>{_NAME}),?\s+(?:for|under|to|against|requesting)\b"
)
_RE_PROJECT_NAME = re.compile(
    r"(?:\bthe\s+|[’']s?\s+)(?P<project>(?:[A-Z0-9][\w&.\-]*\s+){1,7}?Project)\b"
)
_PLACEHOLDER_AUTHORS = {"no organization found", "individual", "individual no affiliation"}


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    # Periods stay: they end "L.P." and "Inc.", not sentences.
    return re.sub(r"\s+", " ", text).strip(" ,") or None


def extract_applicant(hit: dict) -> str | None:
    """Who is asking FERC for something.

    Applicant filings carry the filer as an AUTHOR affiliation. FERC's own
    orders are authored by the Commission, so for those the applicant is read
    out of the description ("... re X's Y Project under CP...").
    """
    if hit.get("category") == "Submittal":
        for affiliation in hit.get("affiliations") or []:
            name = _clean(affiliation.get("affiliation"))
            if affiliation.get("afType") == "AUTHOR" and name and name.lower() not in _PLACEHOLDER_AUTHORS:
                return name
    description = (hit.get("description") or "").strip()
    for pattern in (_RE_RE_POSSESSIVE, _RE_RE_UNDER, _RE_SUBMITS, _RE_OF_APPLICANT):
        match = pattern.search(description)
        if match:
            return _clean(match.group("applicant"))
    return None


def extract_project_name(description: str) -> str | None:
    match = _RE_PROJECT_NAME.search(description or "")
    return _clean(match.group("project")) if match else None


def extract_region(description: str) -> str | None:
    match = _STATE_RE.search(description or "")
    return _STATE_ABBREVIATIONS[match.group(1).lower()] if match else None


def classify_project_type(config: DocketPrefixConfig, description: str) -> str:
    if config.prefix == "CP" and _LNG_RE.search(description or ""):
        return "lng_terminal"
    return config.project_type


def classify_decision(description: str, project_type: str) -> str | None:
    """'approved' / 'denied' if this order decides an application, else None.

    Most orders on a docket are procedural (rehearing, stays, extensions of
    time, compliance). Those return None so that e.g. "Order Denying Rehearing"
    on an approved pipeline is not read as a denial of the pipeline.
    """
    if not _IS_ORDER_RE.search(description or ""):
        return None
    family = "gas" if project_type in ("natural_gas_pipeline", "lng_terminal") else project_type
    if family == "electric_complaint" and _EL_PROCEDURAL_RE.search(description):
        return None
    if _APPROVAL_RES[family].search(description):
        return "approved"
    if _DENIAL_RES[family].search(description):
        return "denied"
    return None


def base_docket_number(docket: str) -> str:
    """'CP16-116-004' -> 'CP16-116'; 'P-2669-102' -> 'P-2669'."""
    head, _, _ = docket.rpartition("-")
    return head or docket


def _sub_docket(docket: str) -> str:
    return docket.rpartition("-")[2]


def _matching_dockets(hit: dict, prefix: str) -> list[str]:
    pattern = re.compile(rf"^{re.escape(prefix)}-?\d")
    return [d for d in hit.get("docketNumbers") or [] if pattern.match(d)]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    month, day, year = value.split("/")
    return date(int(year), int(month), int(day))


def _source_url(hit: dict) -> str | None:
    # "acesssionNumber" is FERC's own spelling of the field.
    accession = hit.get("acesssionNumber") or hit.get("accessionNumber")
    if not accession:
        return None
    return f"https://elibrary.ferc.gov/eLibrary/filelist?accession_number={accession}"


@dataclass
class ActionRecord:
    docket_number: str  # full, with sub-docket: "CP17-101-000"
    project_type: str
    action_type: str
    applicant: str | None = None
    project_name: str | None = None
    region: str | None = None
    filed_date: date | None = None
    decision_date: date | None = None
    outcome: str = "pending"
    source_url: str | None = None

    @property
    def base_docket(self) -> str:
        return base_docket_number(self.docket_number)

    def _fill_identity(self, hit: dict) -> None:
        description = hit.get("description") or ""
        self.applicant = self.applicant or extract_applicant(hit)
        self.project_name = self.project_name or extract_project_name(description)
        self.region = self.region or extract_region(description)


def _is_application(config: DocketPrefixConfig, docket: str, description: str) -> bool:
    if _DRAFT_RE.search(description):
        return False
    if config.prefix == "EL" and _EL_NOT_APPLICATION_RE.search(description):
        return False
    if config.prefix == "CP":
        if _GAS_NOT_APPLICATION_RE.search(description):
            return False
        # Abandonment-only requests (NGA §7(b)) retire facilities; v1 tracks
        # permission to build.
        if _ABANDONMENT_RE.search(description) and not _BUILD_RE.search(description):
            return False
    if config.prefix == "P" and (
        not _HYDRO_APPLICATION_RE.search(description) or _HYDRO_NOT_APPLICATION_RE.search(description)
    ):
        return False
    if config.year_coded and _sub_docket(docket) != "000":
        # Later sub-dockets also collect supplements and compliance filings;
        # only a new request starts a new action.
        return config.amendment_action_type is not None and bool(_REQUEST_RE.search(description))
    return True


def merge_application_hit(
    records: dict[str, ActionRecord], hit: dict, config: DocketPrefixConfig, action_type: str
) -> None:
    description = hit.get("description") or ""
    filed = _parse_date(hit.get("filedDate"))
    for docket in _matching_dockets(hit, config.prefix):
        record = records.get(docket)
        if record is None:
            if not _is_application(config, docket, description):
                continue
            is_original = not config.year_coded or _sub_docket(docket) == "000"
            record = records[docket] = ActionRecord(
                docket_number=docket,
                project_type=classify_project_type(config, description),
                action_type=action_type if is_original else config.amendment_action_type,
            )
        elif record.project_type == "natural_gas_pipeline":
            record.project_type = classify_project_type(config, description)
        if filed and (record.filed_date is None or filed < record.filed_date):
            record.filed_date = filed
            if record.outcome == "pending":
                record.source_url = _source_url(hit)
        record._fill_identity(hit)


def merge_decision_hit(records: dict[str, ActionRecord], hit: dict, config: DocketPrefixConfig) -> None:
    """Attach an order to the application filed under the same sub-docket."""
    description = hit.get("description") or ""
    decided = _parse_date(hit.get("filedDate"))
    for docket in _matching_dockets(hit, config.prefix):
        record = records.get(docket)
        if record is None or decided is None:
            continue
        outcome = classify_decision(description, record.project_type)
        if outcome is None:
            continue
        if record.filed_date and decided < record.filed_date:
            continue
        # The first order that decides the application is the decision; later
        # ones (reissuance after remand, vacatur) don't move the date.
        if record.outcome == "pending" or decided < record.decision_date:
            record.outcome = outcome
            record.decision_date = decided
            record.source_url = _source_url(hit) or record.source_url
        record._fill_identity(hit)


def merge_withdrawal_hit(records: dict[str, ActionRecord], hit: dict, config: DocketPrefixConfig) -> None:
    withdrawn = _parse_date(hit.get("filedDate"))
    for docket in _matching_dockets(hit, config.prefix):
        record = records.get(docket)
        if record is None or record.outcome != "pending" or withdrawn is None:
            continue
        if record.filed_date and withdrawn < record.filed_date:
            continue
        record.outcome = "withdrawn"
        record.decision_date = withdrawn
        record.source_url = _source_url(hit) or record.source_url


def finalize_records(
    records: dict[str, ActionRecord], config: DocketPrefixConfig, start_date: date
) -> list[ActionRecord]:
    """Drop lookback-only records and hydro precursor filings."""
    kept = [
        r
        for r in records.values()
        if (r.filed_date and r.filed_date >= start_date) or (r.decision_date and r.decision_date >= start_date)
    ]
    if config.year_coded:
        return kept

    # A hydro relicensing leaves several license-class filings on different
    # sub-dockets (the final application and whatever preceded it). Keep the
    # decided ones; among undecided ones keep only the latest, and only if it
    # postdates the last decision.
    by_project: dict[str, list[ActionRecord]] = {}
    for record in kept:
        by_project.setdefault(record.base_docket, []).append(record)
    result: list[ActionRecord] = []
    for group in by_project.values():
        decided = [r for r in group if r.outcome != "pending"]
        pending = sorted((r for r in group if r.outcome == "pending"), key=lambda r: r.filed_date)
        result.extend(decided)
        last_decision = max((r.decision_date for r in decided), default=None)
        if pending and (last_decision is None or pending[-1].filed_date > last_decision):
            result.append(pending[-1])
    return result


def fiscal_year_codes(start_date: date, end_date: date) -> list[str]:
    """Docket year codes follow the federal fiscal year, which starts Oct 1."""
    first = start_date.year + (1 if start_date.month >= 10 else 0)
    last = end_date.year + (1 if end_date.month >= 10 else 0)
    return [f"{year % 100:02d}" for year in range(first, last + 1)]


def _build_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def iter_search_hits(
    session: requests.Session,
    docket_number: str,
    categories: list[str],
    document_class: str,
    document_type: str,
    start_date: date,
    end_date: date,
    search_text: str = "*",
    page_size: int = DEFAULT_PAGE_SIZE,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
) -> Iterator[dict]:
    """Yield every hit for one query.

    FERC's result order is not stable from one page request to the next, so
    paging through a large result set repeats some hits and silently drops
    others. Instead, a query that doesn't fit in one page is split by date
    until each piece does.
    """

    def search(start: date, end: date, page: int) -> dict:
        body = {
            "searchText": search_text,
            # A keyword is matched against descriptions only, not document text.
            "searchFullText": search_text == "*",
            "searchDescription": True,
            "dateSearches": [
                {"dateType": "filed_date", "startDate": start.isoformat(), "endDate": end.isoformat()}
            ],
            "availability": None,
            "affiliations": [],
            "categories": categories,
            "libraries": [],
            "accessionNumber": None,
            "eFiling": False,
            "docketSearches": [{"docketNumber": docket_number, "subDocketNumbers": []}],
            "resultsPerPage": page_size,
            "curPage": page,  # 1-based: the API returns page 1 for both 0 and 1
            "classTypes": [{"documentClass": document_class, "documentType": document_type}],
            "sortBy": "",
            "groupBy": "NONE",
            "idolResultID": "",
            "allDates": False,
        }
        data = _post_with_retry(session, body)
        if not data.get("success", True):
            raise RuntimeError(f"FERC search failed for {docket_number}: {data.get('errorMessage')}")
        time.sleep(sleep_seconds)
        return data

    pending = [(start_date, end_date)]
    while pending:
        start, end = pending.pop()
        data = search(start, end, 1)
        hits = data.get("searchHits") or []
        total = data.get("totalHits") or 0
        if total <= len(hits):
            yield from hits
        elif start < end:
            middle = start + (end - start) // 2
            pending += [(middle + timedelta(days=1), end), (start, middle)]
        else:
            # More than a page filed on a single day: paging is all that's left.
            yield from hits
            for page in range(2, -(-total // page_size) + 1):
                yield from search(start, end, page).get("searchHits") or []


def _post_with_retry(session: requests.Session, body: dict, attempts: int = 4) -> dict:
    for attempt in range(attempts):
        try:
            response = session.post(ADVANCED_SEARCH_URL, json=body, timeout=120)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def collect_records_for_prefix(
    session: requests.Session, config: DocketPrefixConfig, start_date: date, end_date: date
) -> list[ActionRecord]:
    lookback_start = date(start_date.year - config.lookback_years, 1, 1)
    docket_queries = (
        [f"{config.prefix}{code}" for code in fiscal_year_codes(start_date, end_date)]
        if config.year_coded
        else [config.prefix]
    )
    records: dict[str, ActionRecord] = {}
    for docket_query in docket_queries:
        for document_type, action_type in config.application_types:
            for hit in iter_search_hits(
                session, docket_query, ["Submittal"], APPLICATION_CLASS, document_type, lookback_start, end_date
            ):
                merge_application_hit(records, hit, config, action_type)

        for query in config.decision_queries:
            for hit in iter_search_hits(
                session,
                docket_query,
                ["Issuance"],
                ORDER_CLASS,
                query.document_type,
                lookback_start,
                end_date,
                search_text=query.search_text,
            ):
                merge_decision_hit(records, hit, config)

        for hit in iter_search_hits(
            session, docket_query, ["Submittal"], *WITHDRAWAL_CLASS, lookback_start, end_date
        ):
            merge_withdrawal_hit(records, hit, config)
        print(f"  {docket_query}: {len(records)} actions so far", flush=True)

    return finalize_records(records, config, start_date)


def upsert_records(db: Session, records: Iterable[ActionRecord]) -> int:
    """Upsert one project per base docket and one agency_action per record."""
    by_project: dict[str, list[ActionRecord]] = {}
    for record in records:
        by_project.setdefault(record.base_docket, []).append(record)

    count = 0
    for base_docket, group in by_project.items():
        group.sort(key=lambda r: (r.filed_date or date.max, r.docket_number))
        applicant = next((r.applicant for r in group if r.applicant), None) or "Unknown"
        name = next((r.project_name for r in group if r.project_name), None) or f"{applicant} ({base_docket})"
        region = next((r.region for r in group if r.region), None)
        project_type = (
            "lng_terminal" if any(r.project_type == "lng_terminal" for r in group) else group[0].project_type
        )

        existing = (
            db.query(AgencyAction)
            .filter(AgencyAction.agency == "FERC", AgencyAction.docket_number.like(f"{base_docket}-%"))
            .first()
        )
        if existing is not None:
            project = existing.project
            project.name, project.applicant, project.project_type = name, applicant, project_type
            project.region = region or project.region
        else:
            project = Project(name=name, applicant=applicant, project_type=project_type, region=region)
            db.add(project)
            db.flush()

        for record in group:
            action = (
                db.query(AgencyAction)
                .filter_by(agency="FERC", docket_number=record.docket_number, action_type=record.action_type)
                .one_or_none()
            )
            if action is None:
                action = AgencyAction(
                    project_id=project.id,
                    agency="FERC",
                    docket_number=record.docket_number,
                    action_type=record.action_type,
                )
                db.add(action)
            action.filed_date = record.filed_date
            action.decision_date = record.decision_date
            action.outcome = record.outcome
            action.source_url = record.source_url
            count += 1
    db.flush()
    return count


def run_ingestion(
    db: Session,
    start_date: date,
    end_date: date,
    prefixes: Iterable[str] | None = None,
    session: requests.Session | None = None,
) -> int:
    session = session or _build_http_session()
    total = 0
    for config in DOCKET_PREFIX_CONFIGS:
        if prefixes and config.prefix not in prefixes:
            continue
        print(f"Fetching {config.prefix} dockets...", flush=True)
        records = collect_records_for_prefix(session, config, start_date, end_date)
        total += upsert_records(db, records)
        db.commit()
    return total


def main(argv: list[str] | None = None) -> None:
    today = date.today()
    parser = argparse.ArgumentParser(description="Ingest FERC eLibrary docket data.")
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=date(today.year - BACKFILL_YEARS, 1, 1),
        help=f"Default: January 1, {BACKFILL_YEARS} years back.",
    )
    parser.add_argument("--end-date", type=date.fromisoformat, default=today)
    parser.add_argument(
        "--docket-types",
        nargs="+",
        choices=[config.prefix for config in DOCKET_PREFIX_CONFIGS],
        help="Limit to specific docket prefixes (default: all).",
    )
    args = parser.parse_args(argv)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        total = run_ingestion(db, args.start_date, args.end_date, prefixes=args.docket_types)
    finally:
        db.close()
    print(f"Upserted {total} agency actions ({args.start_date} to {args.end_date}).")


if __name__ == "__main__":
    main()
