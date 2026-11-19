# Almanac Grid

A federal permitting-outcomes tracker: a normalized database of permitting
decisions (approved / denied / withdrawn, and how long each took) by project
type, so trends are visible over time instead of scattered across agency
dockets. v1 covers FERC; v2 adds BLM rights-of-way (solar, wind, electric
transmission, pipelines) and geothermal leases. See
[docs/plan.md](docs/plan.md) for the why, [docs/spec.md](docs/spec.md) for the
v1 spec and [docs/spec-v2.md](docs/spec-v2.md) for v2.

## Stack

- Python 3.11 / FastAPI, server-rendered Jinja2 pages (no frontend build)
- PostgreSQL via SQLAlchemy

## Local setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
createdb almanac_grid
```

`DATABASE_URL` in `.env` points at the database. Tables are created by the
ingestion script the first time it runs.

## Load FERC data

```bash
.venv/bin/python -m scripts.ingest_ferc
```

With no arguments this backfills the last 10 years (from January 1, ten years
back, to today) for every docket type. It makes a few hundred requests to
FERC's eLibrary search and takes 5 to 10 minutes. It is safe to re-run at
any time, by hand or from cron: rows are upserted, so a pending application
picks up its decision on a later run instead of being duplicated.

```bash
# A narrower window, or only some docket types (CP, P, EL)
.venv/bin/python -m scripts.ingest_ferc --start-date 2022-01-01 --end-date 2024-12-31
.venv/bin/python -m scripts.ingest_ferc --docket-types CP P
```

### What gets ingested

FERC has no bulk API. The script calls the JSON endpoint behind the
[eLibrary](https://elibrary.ferc.gov/eLibrary/search) search page, filtered by
docket prefix, date range and document type. A base docket (`CP17-101`) becomes
a `project`; each sub-docket that has its own application (`CP17-101-000`, a
later `CP17-101-007` petition) becomes an `agency_action`, paired with the
order FERC issued under that same sub-docket.

| Docket prefix | project_type | What counts as an action |
|---|---|---|
| `CP` | `natural_gas_pipeline`, or `lng_terminal` when the filing mentions LNG / liquefaction | Certificate applications and later amendments or petitions |
| `P` | `hydroelectric` | License and relicense applications |
| `EL` | `electric_complaint` | Complaints and petitions for declaratory order |

Outcomes are read from the wording of FERC's order titles ("Order Issuing
Certificate", "Order Denying ...") and from withdrawal notices. Things to know
about the result:

- **Outcomes are heuristic.** Procedural orders (rehearing, stays, extensions,
  compliance) are ignored. An EL order that disposes of the complaint or
  petition without saying which way it went ("Order on Complaint") makes the
  action `outcome_unclear`: it has a decision date and counts toward average
  days to decision, but not toward approval / denial / withdrawal rates, whose
  denominator (`decided`) is known-direction decisions only. An "Order on
  Complaint" that also sets hearing or show-cause procedures hasn't decided the
  case and leaves it `pending`.
- **Not everything on a CP docket is an application.** Blanket-certificate
  prior-notice filings (which take effect without an order) and
  abandonment-only requests are skipped.
- **EL dockets are not transmission permitting.** EL is FERC's general
  electric docket, mostly market and rate disputes, and the search results
  don't say what a given complaint is about, so every original EL complaint or
  petition is ingested under `electric_complaint`.
- **Hydro:** preliminary permits, license transfers and amendments are not
  tracked, and an application filed before the window is only included if its
  decision falls inside it. Filing years before the window therefore show only
  the slow cases; filter the dashboard to the window for comparable numbers.
- **`region`** is a comma-separated list of state codes (`PA,NJ,NY`), since
  pipelines cross state lines; the `region` filter matches any one of them.
  FERC's search results carry no location, so after ingesting, the script
  (`scripts/enrich_regions.py`, also runnable on its own) fills it from public
  lists keyed by docket number: EIA's
  [Natural Gas Pipeline Projects tracker](https://www.eia.gov/naturalgas/data.php)
  for CP dockets; for hydro, FERC's pending-applications and
  licenses-issued-in-under-two-years spreadsheets, plus its Active Licenses
  and Active Exemptions datasets on data.ferc.gov. Those two need a free API
  key from [data.ferc.gov](https://data.ferc.gov/developer/gettingstarted/)
  as `FERC_API_KEY` in `.env`, and are skipped without one. Where a project
  isn't on any list, the state comes from the filing description ("in Greene
  County, Pennsylvania") if it names one. With the key, about 93% of hydro
  projects and half of gas pipelines get a region; smaller certificate work
  (replacements, compressor changes) isn't in EIA's tracker. EL complaints
  have no project location.
- **Rule changes don't remove old rows.** A re-run updates and adds; if you
  change the classification rules in the script, empty the two tables first.
- **Backstop transmission siting** (the new docket type in S.5653) is not
  ingested: the bill is not law and FERC has no such docket yet. The script
  has a marked place to add it as its own docket type.

## Load BLM data

```bash
.venv/bin/python -m scripts.ingest_blm
```

With no arguments this loads every BLM case decided since January 1, ten years
back, plus everything still pending, for all six types. It is safe to re-run:
rows are upserted on (`agency`, `docket_number` = BLM serial, `action_type`),
and both slow sources are cached in the database, so a routine run only
refetches what changed upstream. The first run's schema step adds
`agency_action.review_tier` / `is_major` and the two cache tables
(`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; nothing is dropped).

```bash
# Some types only, or a different window
.venv/bin/python -m scripts.ingest_blm --project-types solar onshore_wind --start-date 2020-01-01
# Fill one source's cache without loading rows (the two can run side by side)
.venv/bin/python -m scripts.ingest_blm --only mlrs
.venv/bin/python -m scripts.ingest_blm --only eplanning
# Rebuild rows from the caches without any MLRS / ePlanning requests
.venv/bin/python -m scripts.ingest_blm --no-mlrs --no-eplanning
```

**Run time.** The first backfill (November 2026) took about 4 hours 20
minutes of wall-clock time with the two sources fetched side by side: MLRS 3
hours 12 minutes (22,600 cases, one request each, throttled to 2 requests per
second; it managed about 2 a second), ePlanning about 4 hours 10 minutes
(17,900 project pages; its server takes 2 to 4 seconds per uncached page, so
it ran at about 1.2 pages a second despite fetching 8 at a time under the same
2-per-second cap), then 3 minutes to load the rows. Both caches commit as they go, so an interrupted backfill
resumes where it stopped. Run the two `--only` commands side by side (they use
different sites), then a plain run to load the rows. After that, a run takes
a few minutes: the GIS layers and ePlanning's project listing are re-read in
full, and only cases whose disposition changed and NEPA projects whose status
changed are refetched (an hour after the backfill, that was 2 cases and 9
projects; with nothing changed upstream, a run makes no MLRS requests).
BLM's GIS server fails some queries at random, answering HTTP 200 with an
`{"error": {"code": 500}}` body (about one in three on Nov 19, 2026); each
layer query is retried up to 8 times, which can stretch the layer fetch from
about a minute to about 4.

### Where BLM data comes from

| Source | What it gives | How it's read |
|---|---|---|
| BLM national case GIS layers ([rights-of-way](https://nlsdbpubgis.blm.gov/arcgis/rest/services/Land_Use_Authorizations/Rights_of_Way_Case_Disp/MapServer), [geothermal leases](https://nlsdbpubgis.blm.gov/arcgis/rest/services/Fluid_Minerals/Geothermal_Leases_Case_Disp/MapServer)) | The case universe: serial, product, customer, states, current disposition (Authorized / Pending / Interim / Closed) and its date | Public ArcGIS REST `query` |
| [MLRS](https://mlrs.blm.gov) case actions | Application date and outcome, from BLM's disposition action codes | Undocumented Salesforce Aura call, one per case, cached in `blm_case_cache` |
| [ePlanning](https://eplanning.blm.gov) (BLM's National NEPA Register) | NEPA review tier (CX / DNA / EA / EIS), joined on the project's Case File Number | Search-results JSON for the project list, then each project's page; cached in `blm_eplanning_cache` |

| `project_type` | BLM products | `action_type` |
|---|---|---|
| `solar` | Solar Development Grant, Solar Development Lease | `row_grant` |
| `onshore_wind` | Wind Development Grant | `row_grant` |
| `electric_transmission` | Power Transmission (FLPMA), Power Transmission Line, Power Facility & Transmission | `row_grant` |
| `oil_gas_pipeline` | Oil and Gas Pipelines, Pipeline-Other | `row_grant` |
| `co2_pipeline` | CO2 Pipeline, Carbon Dioxide Pipelines | `row_grant` |
| `geothermal` | Competitive / noncompetitive geothermal leases (post-2005 rules), direct-use and mining-claimant leases | `geothermal_lease` |

Each BLM case is one `project` (name = the case name, or "customer (serial)";
`region` = the case's states) with one `agency_action` whose `docket_number` is
the MLRS serial (`CSE_NR`) and whose `source_url` is the MLRS case page.
Wind and solar site-testing authorizations, temporary use permits,
reconveyed / acquired lines and the Trans-Alaska Pipeline products are left
out. BLM data can't tell natural gas from oil pipelines or transmission from
distribution lines, so those types are dominated by small lines; `is_major`
(the case has an ePlanning EA or EIS) separates real projects, and the
dashboard shows pipelines and transmission major-only by default. No attempt is
made yet to link a pipeline's BLM right-of-way to its FERC certificate (v2.1).

### How BLM outcomes are decided

- **Filing date** is the earliest *Application Received* action (code 124),
  else *Case Established* (387). Without either it stays empty; it is never
  guessed.
- **Outcome** is the first action, in date order, that grants (307
  *Right-of-Way Issued*; 8307 *Land Use Authorization Issued*, MLRS's own code
  for grants since 2021; 237 *Lease Issued*), rejects (125) or withdraws (130)
  the application, with `decision_date` its date. A case closed (967 / 970)
  without one is `outcome_unclear`. A reinstatement (388) reopens the case and
  later actions decide it. Post-authorization actions (amendments, renewals,
  relinquishments, post-authorization withdrawals or denials) don't change
  the outcome, so a grant that later expired or was relinquished still counts as
  approved on its grant date.
- **New MLRS actions have a status**, and only ones that took effect count: a
  grant or closure still at *Filed* / *Under Review* hasn't happened yet. Every
  migrated LR2000 action counts.
- **When MLRS doesn't decide it**, the GIS layer does: Authorized means
  `approved` on the layer's disposition date, Closed means `outcome_unclear`
  on that date, Pending / Interim mean `pending`.
- **Window:** a case is loaded if it was decided on or after the start date or
  is still pending, judged on the MLRS decision date. Rights-of-way granted
  long ago that were closed (expired, relinquished) inside the window are
  therefore *not* loaded.
- **Review tier** comes from every ePlanning project that lists the case
  (Renewable Energy and Lands & Realty programs, plus Fluid Minerals projects
  whose name mentions geothermal, pipelines or rights-of-way). Serials are
  matched on letters plus the unpadded number, ignoring segment suffixes
  (`AZA-34187` matches `AZA 03418702`). Where several projects list a case,
  the highest tier wins. No match leaves `review_tier` empty (unknown), not
  `none`.
- **Geothermal leases** are leasing decisions, which come before (and aren't)
  a power plant's permits; the UI labels them "Geothermal (leases)".

### Coverage (first backfill, November 2026)

13,304 BLM actions load for January 2016 onward. The spec asked that at least
90% of decided solar and wind cases have both a filing date and a
known-direction outcome. Filing dates clear that bar; known outcomes don't for
solar and wind, because BLM closed those cases without recording why:

| `project_type` | decided | with filing date | known outcome | both | pending |
|---|---|---|---|---|---|
| `solar` | 226 | 95.1% | 80.1% | 77.4% | 260 |
| `onshore_wind` | 34 | 97.1% | 61.8% | 61.8% | 20 |
| `electric_transmission` | 4,261 | 97.8% | 90.5% | 90.0% | 761 |
| `oil_gas_pipeline` | 6,480 | 95.3% | 91.2% | 90.7% | 544 |
| `co2_pipeline` | 1 | 100% | 100% | 100% | 1 |
| `geothermal` | 714 | 98.3% | 96.1% | 94.4% | 3 |

The 45 solar and 13 wind `outcome_unclear` cases are all closures (967
*Closed without action* or 970 *Case Closed*) with no rejection, withdrawal
or grant anywhere in the case's MLRS history, including actions still in
progress: 27 solar and 9 wind were closed in LR2000 before MLRS took over, the
rest in MLRS since. Only 4 solar and 1 wind had been *Held for Rejection*
first, so reading closures as denials would be a guess, not a fix. The 11 solar
cases without a filing date have no *Application Received* or *Case
Established* action at all. Treat solar and wind denial and withdrawal rates
as lower bounds: a fifth of solar and two-fifths of wind decisions are known
only to have ended.

Review tier (criterion 3) is set wherever an ePlanning project's Case File
Number links to the case:

| `project_type` | actions | with review tier | EA or EIS (`is_major`) | decided cases with a tier |
|---|---|---|---|---|
| `solar` | 486 | 74 (15.2%) | 42 | 62 of 226 (27%) |
| `onshore_wind` | 54 | 9 (16.7%) | 6 | 8 of 34 (24%) |
| `electric_transmission` | 5,022 | 930 (18.5%) | 279 | 822 of 4,261 (19%) |
| `oil_gas_pipeline` | 7,024 | 1,182 (16.8%) | 662 | 1,101 of 6,480 (17%) |
| `co2_pipeline` | 2 | 0 | 0 | 0 of 1 |
| `geothermal` | 717 | 36 (5.0%) | 10 | 36 of 714 (5%) |

Match rates are low because ePlanning is a register of NEPA documents BLM
chose to post, not of cases: 28% of the 17,900 candidate projects give no
case file number at all, many pending applications have no NEPA project yet,
and most geothermal EAs cover a lease sale ("Multiple" case numbers) rather
than one lease. Some major projects are also missing from the GIS layers
entirely (Desert Quartzite Solar, CACA 049497, has an ePlanning EIS but no
layer row, presumably because its lands couldn't be mapped), so they aren't
in the case universe. And because segment suffixes are ignored when joining, a
side right-of-way on the same serial as a big project (a tortoise fence for
Yellow Pine Solar) inherits that project's EIS.

### MLRS is the fragile part

MLRS has no API. Case actions come from the same guest Aura endpoint
(`POST https://mlrs.blm.gov/s/sfsites/aura`) the public case pages use,
calling Salesforce's `RelatedListUiController.getRelatedListRecords` for the
`Case_Actions__r` related list with fields such as `Code__c`. That can break
without notice:

- **Salesforce releases** change the Aura framework id. The script reads it
  from a case page at the start of each run and again whenever MLRS answers
  `clientOutOfSync`, so a release mid-run is handled; nothing is hard-coded.
- **BLM can rename fields or related lists,** or stop exposing them to
  guests. The case page's own list view already hides the action code from
  guests; `getRelatedListRecords` with explicit fields is what still returns
  it. If a case's call fails, the error is cached (so it isn't retried every
  run) and the case falls back to its GIS-layer disposition with no filing
  date. A run where most cases fail this way means the endpoint changed; find
  the new call by watching a case page's network requests.
- **Throttle:** at most 2 requests per second. Don't raise it.

## Analyses

One-off analyses live in `analysis/`. They read the local database and print
results; none of them write to it.

- `python -m analysis.s1402_blm [--json PATH]`: a §1402-style check on BLM
  project types. Prints, per type and year since 2016, decisions by outcome,
  the open queue at year end, how much of it is past the bill's deadlines,
  the 5-year baselines the bill compares against, and the share of each
  filing year's applications decided within 12 months. Definitions and
  approximations are in the script's docstring. Behind the November 2026
  write-up "Solar Permitting on BLM Land Under §1402".
- `python -m analysis.major_flag_audit`: where BLM `is_major` flags come
  from: matched on the case's own serial, inherited by a segment from its
  base serial, or taken from a review that started after the decision.
  Re-reads BLM's GIS case layers (a few minutes, public).

## Run the app

```bash
.venv/bin/uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/.

| Page | |
|---|---|
| `/` | Trends dashboard: outcomes and time to decision by project type (FERC and BLM side by side) and filing year |
| `/ui/projects` | Project list and search |
| `/ui/projects/{id}` | Project detail with its full action history |

| JSON API | |
|---|---|
| `GET /trends` | Approval / denial / withdrawal rates and average days to decision per `project_type` and its `agency`, with a per-year breakdown. Filters: `start_date`, `end_date` (filing date), `agency` (`FERC` / `BLM`), `major_only` (BLM pipelines and transmission: only EA / EIS cases; off by default) |
| `GET /projects` | List and search. Filters: `project_type`, `applicant` (substring), `region`, `start_date`, `end_date`, `agency`, plus `limit` / `offset` |
| `GET /projects/{id}` | One project and its `agency_action` history |
| `GET /health` | Liveness check |

Interactive API docs are at `/docs`.

## Tests

```bash
.venv/bin/pytest
```

Tests run against an in-memory SQLite database and saved samples of FERC
search results and BLM GIS, MLRS and ePlanning responses (`tests/fixtures/`);
they never contact FERC, BLM or Postgres.
