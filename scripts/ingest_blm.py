"""Ingest BLM right-of-way and geothermal-lease decisions into the `project` /
`agency_action` tables as `agency = "BLM"` rows. See docs/spec-v2.md.

Sources, in order:

1. BLM's national case GIS layers (ArcGIS REST; Authorized / Pending /
   Interim / Closed) give the case universe: one row per land parcel, so rows
   are collapsed to one case per serial (`CSE_NR`).
2. MLRS case actions (scripts/mlrs.py) give the application date and the
   outcome, decoded from LR2000 disposition action codes. Cached per case in
   `blm_case_cache`; a case is refetched only when its layer disposition or
   disposition date changes.
3. BLM ePlanning (scripts/eplanning.py) gives the NEPA review tier, joined on
   the project's Case File Number. Cached in `blm_eplanning_cache`.

Run manually or via cron. Safe to re-run: rows are upserted, keyed on
(agency, docket_number, action_type).

Usage:
    python -m scripts.ingest_blm
    python -m scripts.ingest_blm --start-date 2020-01-01 --project-types solar onshore_wind
    python -m scripts.ingest_blm --only mlrs        # just fill the MLRS cache
    python -m scripts.ingest_blm --only eplanning   # just fill the ePlanning cache
"""

from __future__ import annotations

import argparse
import datetime
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable

import requests
from sqlalchemy.orm import Session, joinedload

from app.models import AgencyAction, BlmCaseCache, BlmEplanningCache, Project
from scripts.eplanning import EplanningClient, refresh_cache, serial_keys, tiers_by_serial
from scripts.mlrs import CASE_PAGE_URL, MlrsClient, MlrsError

AGENCY = "BLM"
BACKFILL_YEARS = 10
USER_AGENT = "almanac-grid-ingest/2.0"

ARCGIS_BASE = "https://nlsdbpubgis.blm.gov/arcgis/rest/services"
ROW_SERVICE = ARCGIS_BASE + "/Land_Use_Authorizations/Rights_of_Way_Case_Disp/MapServer"
GEOTHERMAL_SERVICE = ARCGIS_BASE + "/Fluid_Minerals/Geothermal_Leases_Case_Disp/MapServer"
LAYERS = {0: "Authorized", 1: "Pending", 2: "Interim", 3: "Closed"}
DECIDED_LAYERS = {"Authorized", "Closed"}
ARCGIS_PAGE_SIZE = 2000


@dataclass(frozen=True)
class ProductGroup:
    project_type: str
    action_type: str
    service: str
    products: tuple[str, ...]


PRODUCT_GROUPS = (
    ProductGroup("solar", "row_grant", ROW_SERVICE, ("Solar Development Grant", "Solar Development Lease")),
    ProductGroup("onshore_wind", "row_grant", ROW_SERVICE, ("Wind Development Grant",)),
    ProductGroup(
        "electric_transmission",
        "row_grant",
        ROW_SERVICE,
        (
            "POWER TRANSMISSION FEDERAL LAND POLICY AND MANAGEMENT ACT",
            "Power Transmission Line",
            "Power Facility & Transmission",
        ),
    ),
    ProductGroup("oil_gas_pipeline", "row_grant", ROW_SERVICE, ("Oil and Gas Pipelines", "Pipeline-Other")),
    ProductGroup("co2_pipeline", "row_grant", ROW_SERVICE, ("CO2 Pipeline", "Carbon Dioxide Pipelines")),
    # Only leases issued under the post-2005 rules can have been issued inside
    # a 10-year window; pre-2005 and "converted" leases are older leases, and
    # utilization sites / sale files aren't leases.
    ProductGroup(
        "geothermal",
        "geothermal_lease",
        GEOTHERMAL_SERVICE,
        (
            "GEOTHERMAL COMPETITIVE LEASE POST 2005",
            "GEOTHERMAL NONCOMPETITIVE LEASE POST 2005",
            "GEOTHERMAL NONCOMPETITIVE DIRECT USE LEASE",
            "GEOTHERMAL MINING CLAIMANT LEASE",
        ),
    ),
)
PROJECT_TYPES = tuple(group.project_type for group in PRODUCT_GROUPS)

# LR2000 disposition action codes (reports.blm.gov/docs/lr2000/CodeDisposition.pdf).
FILED_CODES = ("124",)  # Application Received
ESTABLISHED_CODES = ("387",)  # Case Established: filing-date fallback
DECISION_CODES = {
    "307": "approved",  # Right-of-Way Issued (LR2000-era actions)
    # Land Use Authorization Issued: what MLRS records for grants made since
    # its 2021-22 launch. Not in the LR2000 code table.
    "8307": "approved",
    "237": "approved",  # Lease Issued
    "125": "denied",  # Application Rejected/Denied
    "130": "withdrawn",  # Application Withdrawn
}
CLOSURE_CODES = ("967", "970")  # Closed without action / Case Closed
REOPEN_CODES = ("388",)  # Case Reinstated-Reopened
# Order of same-day actions: a filing, then a reopening, then a decision,
# then a closure.
_SAME_DAY_RANK = {
    **{code: 0 for code in FILED_CODES + ESTABLISHED_CODES},
    **{code: 1 for code in REOPEN_CODES},
    **{code: 2 for code in DECISION_CODES},
    **{code: 3 for code in CLOSURE_CODES},
}
# A case action's Status__c says whether it took effect. Migrated LR2000
# actions are all "Migrated Case Action". New MLRS actions move through
# Filed / Received / Requested / Under Review to Approved/Accepted or
# Completed; an "issued" or "closed" action still at Filed hasn't happened
# yet. A rejection's own status is "Rejected/Denied" and a withdrawal counts
# once the applicant has filed it.
FINAL_STATUSES = frozenset({"", "migrated case action", "approved/accepted", "completed"})
STATUSES_BY_CODE = {
    "125": FINAL_STATUSES | {"rejected/denied"},
    "130": FINAL_STATUSES | {"withdrawn", "filed", "received", "requested"},
}
# A rejection or withdrawal still in progress (Filed, Under Review, ...) that
# is followed by the case being closed did take effect: it says why the case
# was closed.
TENTATIVE_CODES = ("125", "130")
VOID_STATUSES = frozenset({"draft", "cancelled", "canceled"})
# A filing is a fact whatever later happened to the action record.
ANY_STATUS_CODES = FILED_CODES + ESTABLISHED_CODES


# --------------------------------------------------------------------------
# Source 1: GIS case layers


@dataclass
class BlmCase:
    cse_nr: str
    project_type: str
    action_type: str
    product: str
    disposition: str  # layer name: Authorized / Pending / Interim / Closed
    sf_id: str | None = None
    legacy_serial: str | None = None  # LEG_CSE_NR, as published
    cse_disp_dt: date | None = None
    case_name: str | None = None
    customer: str | None = None
    states: list[str] = field(default_factory=list)

    @property
    def source_url(self) -> str | None:
        return CASE_PAGE_URL.format(sf_id=self.sf_id) if self.sf_id else None

    @property
    def serial_keys(self) -> set[str]:
        return serial_keys(self.legacy_serial) | serial_keys(self.cse_nr)


def _epoch_ms_to_date(value) -> date | None:
    if value in (None, ""):
        return None
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=value)).date()


def _clean(value) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _customer_rank(attributes: dict) -> tuple:
    relationship = (attributes.get("INT_REL_SEC") or "").lower()
    return (relationship in ("applicant", "holder"), attributes.get("PCT_INT_SEC") or 0)


def collapse_parcels(rows: Iterable[tuple[str, dict]], group: ProductGroup) -> dict[str, BlmCase]:
    """Collapse parcel rows (layer name, attributes) into one BlmCase per
    CSE_NR, unioning their states. A case listed in more than one layer
    keeps the most recent disposition."""
    cases: dict[str, BlmCase] = {}
    best_customer: dict[str, tuple] = {}
    for layer, attributes in rows:
        cse_nr = _clean(attributes.get("CSE_NR"))
        if not cse_nr:
            continue
        disp_dt = _epoch_ms_to_date(attributes.get("CSE_DISP_DT"))
        case = cases.get(cse_nr)
        if case is None:
            case = cases[cse_nr] = BlmCase(
                cse_nr=cse_nr,
                project_type=group.project_type,
                action_type=group.action_type,
                product=_clean(attributes.get("BLM_PROD")) or "",
                disposition=layer,
                cse_disp_dt=disp_dt,
            )
        elif layer != case.disposition and (disp_dt or date.min) > (case.cse_disp_dt or date.min):
            case.disposition, case.cse_disp_dt = layer, disp_dt
        case.sf_id = case.sf_id or _clean(attributes.get("SF_ID"))
        case.legacy_serial = case.legacy_serial or _clean(attributes.get("LEG_CSE_NR"))
        case.case_name = case.case_name or _clean(attributes.get("CSE_NAME"))
        customer = _clean(attributes.get("CUST_NM_SEC"))
        if customer and (cse_nr not in best_customer or _customer_rank(attributes) > best_customer[cse_nr]):
            case.customer = customer
            best_customer[cse_nr] = _customer_rank(attributes)
        for state in (_clean(attributes.get("GEO_STATE")) or "").replace(",", " ").split():
            if state not in case.states:
                case.states.append(state)
    return cases


def _arcgis_where(group: ProductGroup, layer_name: str, start_date: date) -> str:
    products = ", ".join("'" + product.replace("'", "''") + "'" for product in group.products)
    where = f"BLM_PROD IN ({products})"
    # The geothermal layers have no disposition date; MLRS decides the window.
    if layer_name in DECIDED_LAYERS and group.service == ROW_SERVICE:
        where += f" AND (CSE_DISP_DT >= DATE '{start_date.isoformat()}' OR CSE_DISP_DT IS NULL)"
    return where


def _arcgis_fields(group: ProductGroup) -> str:
    fields = ["CSE_NR", "LEG_CSE_NR", "SF_ID", "BLM_PROD", "CSE_NAME", "GEO_STATE"]
    if group.service == ROW_SERVICE:
        fields += ["CSE_DISP_DT", "CUST_NM_SEC", "INT_REL_SEC", "PCT_INT_SEC"]
    return ",".join(fields)


def iter_layer_rows(session: requests.Session, service: str, layer: int, where: str, out_fields: str):
    offset = 0
    while True:
        params = {
            "f": "json",
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": ARCGIS_PAGE_SIZE,
        }
        body = _get_json(session, f"{service}/{layer}/query", params)
        if "error" in body:
            raise RuntimeError(f"ArcGIS query failed on {service}/{layer}: {body['error']}")
        features = body.get("features", [])
        for feature in features:
            yield feature["attributes"]
        if not features or not body.get("exceededTransferLimit") and len(features) < ARCGIS_PAGE_SIZE:
            return
        offset += len(features)


def _get_json(session: requests.Session, url: str, params: dict, attempts: int = 4) -> dict:
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, timeout=120)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def fetch_cases(
    session: requests.Session, start_date: date, project_types: Iterable[str] | None = None, log=print
) -> list[BlmCase]:
    cases: list[BlmCase] = []
    for group in PRODUCT_GROUPS:
        if project_types and group.project_type not in project_types:
            continue
        rows = []
        for layer, layer_name in LAYERS.items():
            where = _arcgis_where(group, layer_name, start_date)
            rows += [(layer_name, attrs) for attrs in iter_layer_rows(session, group.service, layer, where, _arcgis_fields(group))]
        collapsed = collapse_parcels(rows, group)
        log(f"  {group.project_type}: {len(rows)} parcel rows -> {len(collapsed)} cases")
        cases.extend(collapsed.values())
    return cases


# --------------------------------------------------------------------------
# Source 2: MLRS case actions -> filing date and outcome


@dataclass
class Decision:
    filed_date: date | None = None
    outcome: str | None = None  # None = MLRS doesn't say
    decision_date: date | None = None


def _parse_iso(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def action_date(action: dict) -> date | None:
    """When the action happened. Migrated LR2000 actions carry the same date
    in every field; new MLRS actions leave Date_Filed blank on some codes."""
    for name in ("Action_Decision_Date__c", "Effective_Date__c", "Action_Date__c", "Date_Filed__c"):
        parsed = _parse_iso(action.get(name))
        if parsed:
            return parsed
    return None


def _status(action: dict) -> str:
    return (action.get("Status__c") or "").strip().lower()


def _counts(code: str, action: dict) -> bool:
    status = _status(action)
    if code in ANY_STATUS_CODES:
        return status not in VOID_STATUSES
    return status in STATUSES_BY_CODE.get(code, FINAL_STATUSES)


def decode_actions(actions: Iterable[dict]) -> Decision:
    """Filing date and outcome of a case's original application.

    Filing date: the earliest Application Received (124), else Case
    Established (387). Outcome: the first right-of-way-issued (307 / 8307) /
    lease-issued / rejected / withdrawn action that took effect, read in date
    order. A case closed (967 / 970) without one is `outcome_unclear`
    (closed, direction unknown), unless a rejection or withdrawal was in
    progress when it closed. A reinstatement (388) reopens the case, so
    dispositions after it win. Post-authorization actions (modifications,
    renewals, relinquishments, ...) are ignored."""
    dated = []
    for action in actions:
        code = (action.get("Code__c") or "").strip()
        when = action_date(action)
        if not code or not when:
            continue
        if _counts(code, action):
            dated.append((when, _SAME_DAY_RANK.get(code, 9), code, True))
        elif code in TENTATIVE_CODES and _status(action) not in VOID_STATUSES:
            dated.append((when, _SAME_DAY_RANK[code], code, False))
    dated.sort()

    result = Decision()
    filings = [when for when, _, code, _ in dated if code in FILED_CODES]
    established = [when for when, _, code, _ in dated if code in ESTABLISHED_CODES]
    result.filed_date = (filings or established or [None])[0]

    tentative: tuple[str, date] | None = None
    for when, _, code, effective in dated:
        if code in REOPEN_CODES:
            result.outcome, result.decision_date, tentative = None, None, None
        elif code in DECISION_CODES and not effective:
            tentative = tentative or (DECISION_CODES[code], when)
        elif code in DECISION_CODES:
            if result.outcome in (None, "outcome_unclear"):
                result.outcome, result.decision_date = DECISION_CODES[code], when
        elif code in CLOSURE_CODES and result.outcome is None:
            result.outcome, result.decision_date = tentative or ("outcome_unclear", when)
    return result


def resolve_outcome(case: BlmCase, payload: dict | None) -> Decision:
    """Combine MLRS actions with the layer disposition. MLRS wins when it has
    a known-direction decision; otherwise the layer says what it can: an
    Authorized case was approved (on its disposition date), a Closed one was
    decided in an unknown direction, Pending/Interim ones are pending. A filing
    date only ever comes from MLRS."""
    decoded = decode_actions((payload or {}).get("actions") or [])
    if decoded.outcome in ("approved", "denied", "withdrawn"):
        return decoded
    if case.disposition == "Authorized":
        return Decision(decoded.filed_date, "approved", case.cse_disp_dt)
    if decoded.outcome == "outcome_unclear":
        return decoded
    if case.disposition == "Closed":
        return Decision(decoded.filed_date, "outcome_unclear", case.cse_disp_dt)
    return Decision(decoded.filed_date, "pending", None)


def in_window(decision: Decision, start_date: date) -> bool:
    if decision.outcome == "pending":
        return True
    if decision.decision_date is not None:
        return decision.decision_date >= start_date
    return decision.filed_date is not None and decision.filed_date >= start_date


def _cache_is_current(entry: BlmCaseCache | None, case: BlmCase) -> bool:
    return (
        entry is not None
        and entry.payload is not None
        and entry.cse_disp == case.disposition
        and entry.cse_disp_dt == case.cse_disp_dt
    )


def refresh_case_cache(
    db: Session,
    cases: list[BlmCase],
    client: MlrsClient | None,
    log=print,
    commit_every: int = 50,
) -> int:
    """Fetch MLRS actions for every case whose cached copy is missing or out of
    date. With no client, only reports what would be fetched. Commits as it
    goes, so a long backfill can be stopped and resumed. Returns cases
    fetched."""
    cached = {entry.cse_nr: entry for entry in db.query(BlmCaseCache)}
    todo = [case for case in cases if case.sf_id and not _cache_is_current(cached.get(case.cse_nr), case)]
    log(f"  MLRS: {len(todo)} cases to fetch ({len(cases) - len(todo)} cached or without an MLRS id)")
    if client is None or not todo:
        return 0
    started = time.monotonic()
    fetched = 0
    for case in todo:
        try:
            payload = client.fetch_case(case.sf_id, with_customers=case.customer is None)
        except MlrsError as error:
            # Cached too, so a case MLRS can't serve isn't retried every run;
            # it's refetched when its disposition changes.
            payload = {"error": str(error)[:500]}
        except requests.RequestException as error:
            log(f"  MLRS {case.cse_nr}: skipped ({error})")
            continue
        entry = cached.get(case.cse_nr) or BlmCaseCache(cse_nr=case.cse_nr)
        entry.sf_id = case.sf_id
        entry.fetched_at = datetime.datetime.utcnow()
        entry.cse_disp = case.disposition
        entry.cse_disp_dt = case.cse_disp_dt
        entry.payload = payload
        cached[case.cse_nr] = db.merge(entry)
        fetched += 1
        if fetched % commit_every == 0:
            db.commit()
            rate = fetched / (time.monotonic() - started)
            remaining = (len(todo) - fetched) / rate / 60 if rate else 0
            log(f"  MLRS: {fetched}/{len(todo)} cases, ~{remaining:.0f} min left")
    db.commit()
    return fetched


def _customer_from_mlrs(payload: dict | None) -> str | None:
    for customer in (payload or {}).get("customers") or []:
        name = _clean(customer.get("Account__r.Name"))
        if name:
            return name
    return None


# --------------------------------------------------------------------------
# Upsert


@dataclass
class BlmRecord:
    case: BlmCase
    decision: Decision
    applicant: str
    review_tier: str | None = None

    @property
    def is_major(self) -> bool:
        return self.review_tier in ("ea", "eis")


def build_records(
    cases: Iterable[BlmCase],
    payloads: dict[str, dict | None],
    tiers: dict[str, dict],
    start_date: date,
) -> list[BlmRecord]:
    records = []
    for case in cases:
        payload = payloads.get(case.cse_nr)
        decision = resolve_outcome(case, payload)
        if not in_window(decision, start_date):
            continue
        applicant = case.customer or _customer_from_mlrs(payload) or "Unknown"
        tier = next((tiers[key]["review_tier"] for key in sorted(case.serial_keys) if key in tiers), None)
        records.append(BlmRecord(case, decision, applicant, tier))
    return records


def upsert_records(db: Session, records: Iterable[BlmRecord]) -> int:
    """One project and one agency_action per BLM case, keyed on
    (agency, docket_number = CSE_NR, action_type)."""
    existing = {
        (action.docket_number, action.action_type): action
        for action in db.query(AgencyAction).options(joinedload(AgencyAction.project)).filter(AgencyAction.agency == AGENCY)
    }
    count = 0
    for record in records:
        case = record.case
        name = case.case_name or f"{record.applicant} ({case.cse_nr})"
        region = ",".join(case.states) or None
        action = existing.get((case.cse_nr, case.action_type))
        if action is None:
            project = Project(name=name, applicant=record.applicant, project_type=case.project_type, region=region)
            db.add(project)
            db.flush()
            action = AgencyAction(
                project_id=project.id,
                agency=AGENCY,
                docket_number=case.cse_nr,
                action_type=case.action_type,
            )
            db.add(action)
        else:
            project = action.project
            project.name, project.applicant, project.project_type = name, record.applicant, case.project_type
            project.region = region or project.region
        action.filed_date = record.decision.filed_date
        action.decision_date = record.decision.decision_date
        action.outcome = record.decision.outcome
        action.source_url = case.source_url
        action.review_tier = record.review_tier
        action.is_major = record.is_major
        count += 1
    db.flush()
    return count


# --------------------------------------------------------------------------


def _build_http_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def run_ingestion(
    db: Session,
    start_date: date,
    project_types: Iterable[str] | None = None,
    session: requests.Session | None = None,
    mlrs_client: MlrsClient | None = None,
    eplanning_client: EplanningClient | None = None,
    only: str | None = None,
    log: Callable[[str], None] = print,
) -> int:
    session = session or _build_http_session()
    if only != "eplanning":
        log("Fetching BLM case layers...")
        cases = fetch_cases(session, start_date, project_types, log=log)
        log("Fetching MLRS case actions...")
        refresh_case_cache(db, cases, mlrs_client, log=log)
    if only != "mlrs" and eplanning_client is not None:
        log("Fetching ePlanning projects...")
        refresh_cache(db, eplanning_client, log=log)
    if only:
        return 0

    payloads = {entry.cse_nr: entry.payload for entry in db.query(BlmCaseCache)}
    tiers = tiers_by_serial(entry.payload or {} for entry in db.query(BlmEplanningCache))
    records = build_records(cases, payloads, tiers, start_date)
    total = upsert_records(db, records)
    db.commit()
    return total


def main(argv: list[str] | None = None) -> None:
    today = date.today()
    parser = argparse.ArgumentParser(description="Ingest BLM right-of-way and geothermal lease decisions.")
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=date(today.year - BACKFILL_YEARS, 1, 1),
        help=f"Cases decided on/after this date, plus everything pending. Default: January 1, {BACKFILL_YEARS} years back.",
    )
    parser.add_argument("--project-types", nargs="+", choices=PROJECT_TYPES, help="Default: all.")
    parser.add_argument(
        "--only",
        choices=("mlrs", "eplanning"),
        help="Only refresh one source's cache, then stop (the two can run side by side).",
    )
    parser.add_argument("--no-mlrs", action="store_true", help="Use cached MLRS actions only; make no MLRS requests.")
    parser.add_argument("--no-eplanning", action="store_true", help="Use cached ePlanning pages only.")
    args = parser.parse_args(argv)

    from app.database import SessionLocal, engine
    from scripts.blm_schema import migrate

    migrate(engine)
    mlrs_client = None if args.no_mlrs else MlrsClient()
    eplanning_client = None if args.no_eplanning else EplanningClient()
    db = SessionLocal()
    started = time.monotonic()
    try:
        total = run_ingestion(
            db,
            args.start_date,
            project_types=args.project_types,
            mlrs_client=mlrs_client,
            eplanning_client=eplanning_client,
            only=args.only,
        )
    finally:
        db.close()
    minutes = (time.monotonic() - started) / 60
    requests_made = (mlrs_client.requests_made if mlrs_client else 0, eplanning_client.requests_made if eplanning_client else 0)
    print(f"MLRS requests: {requests_made[0]}, ePlanning requests: {requests_made[1]}, {minutes:.1f} min.")
    if not args.only:
        print(f"Upserted {total} BLM agency actions (decided since {args.start_date}, or pending).")


if __name__ == "__main__":
    main()
