# Almanac Grid — v2 Technical Spec (BLM)

See `plan.md` for the why (especially "v2: BLM data" and "Why the data model
isn't FERC-shaped"). This is the concrete build target. v1 (`spec.md`) is
built and running; extend it, don't restructure it.

## Goal

Add Bureau of Land Management right-of-way (ROW) decisions — solar, wind,
electric transmission, and pipelines on federal land — plus geothermal leases,
as `agency = "BLM"` rows in the existing `project` / `agency_action` model, with
the same outcome and timing semantics as FERC so the trends dashboard compares
them directly. Record each action's NEPA review tier, which v1.5's §1402
delay analysis needs for every agency.

## Starting point

Everything in v1 exists and works: `scripts/ingest_ferc.py` (FERC eLibrary
ingestion, upserts), `scripts/enrich_regions.py` (fills FERC project regions
from EIA/FERC lists), `app/queries.py` (trends, list, detail),
Jinja2 UI, 88 passing tests on in-memory SQLite. Read the README's "What gets
ingested" section first — it explains the outcome conventions this spec reuses
(`outcome_unclear`, comma-separated `region`).

## Sources — all verified reachable without auth (Nov 2026)

### 1. BLM national ROW case layers (ArcGIS REST) — the case universe

`https://nlsdbpubgis.blm.gov/arcgis/rest/services/Land_Use_Authorizations/Rights_of_Way_Case_Disp/MapServer`
— layers `0` Authorized, `1` Pending, `2` Interim, `3` Closed. Standard
ArcGIS `query` endpoint (`f=json`, `where`, `outFields`, `returnGeometry=false`,
`resultOffset` / `resultRecordCount` up to 2000, `returnDistinctValues`,
`outStatistics` all work).

- Fields: `CSE_NR` (MLRS serial), `LEG_CSE_NR` (legacy LR2000 serial),
  `BLM_PROD` (product), `CMMDTY` (commodity), `CSE_DISP` (disposition),
  `CSE_DISP_DT`, `CSE_DISP_ACTION`, `CUST_NM_SEC` (customer), `CSE_NAME`,
  `GEO_STATE`, `SF_ID` (MLRS Salesforce record id).
- **Rows are land parcels, not cases.** Collapse to one record per `CSE_NR`
  (union `GEO_STATE` values into `region`).
- No application date, and `CSE_DISP_ACTION` is nearly always blank for cases
  migrated from LR2000, so "Closed" alone doesn't say rejected vs withdrawn.
  Both come from source 2.

Product filters (`BLM_PROD`) and in-window case counts (decided since
2016-01-01, or pending):

| v2 project_type | `BLM_PROD` values | authorized | pending | closed |
|---|---|---|---|---|
| `solar` | `Solar Development Grant`, `Solar Development Lease` | 48 | 265 | 177 |
| `onshore_wind` | `Wind Development Grant` | 14 | 22 | 37 |
| `electric_transmission` | `POWER TRANSMISSION FEDERAL LAND POLICY AND MANAGEMENT ACT`, `Power Transmission Line`, `Power Facility & Transmission` | 5,072 | 799 | 2,763 |
| `oil_gas_pipeline` | `Oil and Gas Pipelines`, `Pipeline-Other` | 7,272 | 571 | 3,478 |
| `co2_pipeline` | `CO2 Pipeline`, `Carbon Dioxide Pipelines` | tiny | | |

Excluded: site-testing products (`Wind Project Test`, `Wind Site Test`,
`Solar Site Test`, `Solar Project Area Test`) — they're met-tower/testing
authorizations, not projects; temporary use permits; reconveyed/acquired
products; Trans-Alaska pipeline products (one-off legacy system).

`CMMDTY` can't split natural gas from oil pipelines (94% are `OIL & GAS
FACILITIES`) or transmission from distribution lines (75% `OTHER ENERGY
FACILITIES`), hence the combined `oil_gas_pipeline` type and the `is_major`
flag below.

Geothermal: `Fluid_Minerals/Geothermal_Leases_Case_Disp/MapServer` (same four
layers, same fields — verify) → `project_type = "geothermal"`,
`action_type = "geothermal_lease"`. Leasing precedes, and isn't, the power
plant's permit; label it that way in the UI.

### 2. MLRS case actions — application date and outcome

Each case has a public page at `https://mlrs.blm.gov/s/blm-case/{SF_ID}/x`
(`SF_ID` from source 1; every case is also listed in
`https://mlrs.blm.gov/s/sitemap-blm_case__c-N.xml`). Its Related Records tab
lists Case Actions with Date Filed, Action Date, Effective Date, Status and an
LR2000 action code. The site is Salesforce Experience Cloud: the data comes
from guest Aura calls (`POST /s/sfsites/aura`, controller
`RelatedListViewDataManager.getItems`). Find the exact request by watching the
page's network calls, as v1 did for FERC eLibrary.

Decode with BLM's disposition action codes
(`https://reports.blm.gov/docs/lr2000/CodeDisposition.pdf`, verified):

| code | meaning | use |
|---|---|---|
| 124 | Application Received | `filed_date` |
| 387 | Case Established | `filed_date` fallback when no 124 |
| 307 | Right-of-Way Issued | outcome `approved`, `decision_date` |
| 237 | Lease Issued | outcome `approved` (geothermal, solar leases) |
| 125 | Application Rejected/Denied | outcome `denied` |
| 130 | Application Withdrawn | outcome `withdrawn` |
| 967 / 970 | Closed without action / Case Closed | outcome `outcome_unclear` |
| 388 | Case Reinstated-Reopened | reopens: later dispositions win |

Post-authorization actions (304 modified, 308 renewed, 310 relinquished,
234 expired, 037/039 post-authorization application withdrawn/denied) don't
change the original application's outcome; ignore them for v2.

**This endpoint is the fragile part of v2.** Salesforce releases change the
Aura framework id; the script must read it from the page each run, not
hard-code it. Cache raw action lists per case in a new `blm_case_cache`
table (`cse_nr`, `fetched_at`, `cse_disp_dt`, `payload` JSON) and refetch a
case only when its `CSE_DISP_DT` from source 1 has changed, so routine runs
are incremental. The first backfill is ~20k cases; throttle (≤2 requests/s),
make it resumable, and expect hours — a good job for the mini once it's back
online.

### 3. BLM ePlanning (National NEPA Register) — review tier

Project pages are server-rendered: `https://eplanning.blm.gov/Project-Home/?id=<guid>`,
enumerable from the site's sitemap (~15 pages × ≤5,000 project URLs, all
programs). Fields on the page include NEPA Number (e.g.
`DOI-BLM-AZ-C020-2022-0006-EIS`), Project Type (`CX` / `DNA` / `EA` / `EIS`),
NEPA Status, Applicant, **Case File Number** (an MLRS legacy serial, e.g.
`AZA037939`), Program / Sub-Program, Start Date, NOI date, Decision Date, ROD
date, counties and state.

- Join to cases on Case File Number ↔ `LEG_CSE_NR` (strip spaces, uppercase)
  or `CSE_NR`. One ePlanning project can list several case numbers.
- Keep only the Renewable Energy, Lands & Realty and Fluid Minerals programs.
- The search page has a "Download Results" export whose format is
  unverified; use it if it's simpler than page-parsing.
- Not every case goes through a separately documented NEPA review; no match
  means `review_tier = NULL`, not `none`.

## Data model changes

`agency_action` gains:

| field | type | notes |
|---|---|---|
| review_tier | string, nullable | `cx` / `dna` / `ea` / `eis` / `none`; NULL = unknown. FERC rows stay NULL until v1.5 fills them |
| is_major | bool, default false | true when the action has an EA or EIS. Lets trends separate real projects from the thousands of small gathering/distribution lines |

New `project_type` values: `solar`, `onshore_wind`, `geothermal`,
`electric_transmission`, `oil_gas_pipeline`, `co2_pipeline`.
(`electric_transmission` was retired from FERC data in v1 because EL dockets
aren't siting; BLM transmission ROWs genuinely are.)

BLM row conventions:

- One `project` per BLM case (`CSE_NR`); `name` = `CSE_NAME` or
  "`CUST_NM_SEC` (`CSE_NR`)"; `applicant` = `CUST_NM_SEC`; `region` = the
  case's `GEO_STATE` codes, comma-separated.
- One `agency_action` per case application: `agency = "BLM"`,
  `docket_number = CSE_NR`, `action_type` = `row_grant` (or
  `geothermal_lease`), dates/outcome per source 2, `source_url` = the MLRS
  case page.
- Outcome when MLRS actions are missing or unreadable: from source 1's
  layer — Authorized → `approved` with `decision_date = CSE_DISP_DT`;
  Closed → `outcome_unclear`; Pending/Interim → `pending`; `filed_date`
  NULL. Never guess a filing date.
- Window: same 10-year backfill as FERC — cases decided on/after
  January 1 ten years back, plus everything pending.

Schema migration: there's no Alembic yet. Add the two columns with an
idempotent step at ingestion start (Postgres
`ALTER TABLE agency_action ADD COLUMN IF NOT EXISTS ...`); tests keep using
`create_all`. Don't introduce Alembic for two columns.

## Ingestion

New `scripts/ingest_blm.py`, same shape as `ingest_ferc.py`: idempotent
upsert keyed on (`agency`, `docket_number`, `action_type`), CLI flags for
`--start-date` and `--project-types`, run by hand or cron. Order: source 1
(cases) → source 2 (actions, cached) → source 3 (review tier) → upsert.

Interactions with existing code to get right:

- `scripts/enrich_regions.py` must only touch FERC actions — BLM regions come
  from `GEO_STATE` and are already authoritative.
- `upsert_records` in `ingest_ferc.py` finds existing projects by FERC docket
  prefix; make sure nothing there can match a BLM serial.

## API and UI

- `agency` filter (`FERC` / `BLM` / all) on `GET /trends` and
  `GET /projects`, and on both pages.
- `major_only` filter on `/trends` (uses `is_major`); the dashboard shows
  pipelines and transmission with major-only on by default, with a toggle.
- Trends rows stay one per `project_type`; label each with its agency.
- Project detail: show agency, BLM serial, review tier, and link to MLRS.
- Geothermal labeled "Geothermal (leases)".

## Acceptance criteria

1. `ingest_blm.py` loads all five ROW types and geothermal leases for the
   10-year window into `project` / `agency_action` with `agency = "BLM"`.
2. At least 90% of in-window decided solar and wind cases have a
   `filed_date` and an outcome other than `outcome_unclear`, or the README
   explains with numbers why not. Report the same numbers for pipelines and
   transmission.
3. `review_tier` / `is_major` are set wherever an ePlanning project links to
   the case; report the match rate per type.
4. A re-run with no upstream changes makes no MLRS requests (cache works).
5. `/trends` and `/projects` filter by agency; the dashboard compares FERC
   and BLM types side by side; existing FERC behavior unchanged.
6. Tests (no live network): parcel-to-case collapsing, action-code → outcome
   mapping including reopen and closed-without-reason, fallback from layer
   disposition, ePlanning page parsing against a saved sample, the
   Case File Number join, the agency and major-only filters.
7. README documents the sources, run time of the first backfill, and the
   fragility of the MLRS endpoint.

## Out of scope for v2

- **Cross-agency matching** (FERC CP docket ↔ BLM pipeline ROW for the same
  physical project). No shared identifier exists; matching is fuzzy on
  applicant, state and dates (e.g. Ruby Pipeline, CP09-54, appears as BLM ROWs
  with commodity `INTERSTATE ENERGY FACILITIES`). Planned as v2.1; v2 leaves
  them as separate projects.
- **§1402 pattern detection** (v1.5 — now built after v2, on both agencies).
- APDs (drilling permits): only aggregate statistics are public.
- Oil & gas leasing, mining plans of operations, Forest Service / Army Corps.
- BLM's reporting dashboards at reports.blm.gov (bulk case-action reports):
  they require accepting a DOI system-use notice, which is the project owner's
  call, not the build agent's.
