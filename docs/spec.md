# Almanac Grid — v1 Technical Spec

See `plan.md` for the why and the scope rationale. This is the concrete build
target.

## Starting point — build on this, don't restructure it

The repo already has a scaffold:

- `app/main.py` — FastAPI app, currently just a `/health` endpoint
- `app/database.py` — SQLAlchemy engine/session/`Base`
- `app/config.py` — settings via `pydantic-settings`, reads `DATABASE_URL`
- `tests/` — pytest set up and passing
- `requirements.txt` / `requirements-dev.txt`
- `.mini/setup.sh` — mini worker provisioning (already fixed for the mini's
  Catalina toolchain quirks; don't change the interpreter path without reason)

## Data model

### `project`

| field | type | notes |
|---|---|---|
| id | pk | |
| name | string | |
| project_type | string | normalized category — `natural_gas_pipeline`, `lng_terminal`, `hydroelectric`, `electric_transmission` |
| applicant | string | |
| region | string | state/region only — no parcel-level geometry in v1 |
| created_at | datetime | |

### `agency_action`

| field | type | notes |
|---|---|---|
| id | pk | |
| project_id | fk → project.id | one project can have multiple actions (amendments, rehearing orders), even within FERC alone |
| agency | string | `"FERC"` for every row in v1 — field exists so v2 can add other agencies without a migration |
| docket_number | string | |
| action_type | string | e.g. `certificate_application`, `license_order`, `backstop_siting_petition` |
| filed_date | date | |
| decision_date | date, nullable | |
| outcome | string | `approved` / `denied` / `withdrawn` / `pending` |
| statutory_deadline | date, nullable | for delay calculations once relevant deadlines are tracked |
| source_url | string | link back to the FERC eLibrary record |

Dedupe projects by docket number + applicant + name when a docket maps
cleanly to one; where FERC issues multiple related dockets for what's
obviously the same physical project, use judgment rather than forcing a
rigid matching rule for v1.

## Data source (v1: FERC only)

[FERC eLibrary](https://elibrary.ferc.gov) docket search, filtered to:

- **CP dockets** — Natural Gas Act §7 certificates (pipelines)
- **LNG-specific dockets** — terminal export/import orders
- **P dockets** — hydroelectric licensing/relicensing under the FPA
- **EL dockets**, and the new backstop transmission-siting docket type under
  FPA §216/§224 — track this one separately from ordinary EL dockets so
  pre- and post-bill siting authority don't get silently conflated if/when
  the bill passes

No official bulk API — write a scraper against the docket search UI (date
range + docket-type query parameters), parse each docket's filing/decision
history, and normalize into `project` + `agency_action` rows.

**Historical backfill target: last 10 years.** Gives trend views real depth
without an unbounded scraping job. Revisit if earlier history turns out to
be cheap to add once the scraper exists.

## Ingestion

A standalone script (`scripts/ingest_ferc.py`), run manually or via cron —
not a live background service for v1. Must be idempotent: safe to re-run,
upserts rather than duplicating rows.

## API

- `GET /trends` — aggregate approval/denial/withdrawal rates and average
  time-to-decision, grouped by `project_type`, filterable by date range
- `GET /projects` — list/search projects (by type, applicant, region, date
  range)
- `GET /projects/{id}` — project detail + full `agency_action` history
- `GET /health` — already exists

## Minimal web UI

Server-rendered via Jinja2 templates through FastAPI — no separate frontend
build step:

- Trends dashboard (a table, plus a simple chart via Chart.js from a CDN is
  fine) of outcomes by project type over time
- Project list/search page
- Project detail page

## Acceptance criteria

1. The ingestion script pulls FERC docket data for the categories above
   across the 10-year window and stores normalized `project` +
   `agency_action` records.
2. `/trends`, `/projects`, and `/projects/{id}` all return real data from the
   ingested set — not fixtures.
3. The web UI renders the trends dashboard and project list/detail using the
   API, not hardcoded data.
4. Tests cover: model creation, at least one ingestion-parsing unit test
   against a saved sample docket page (don't hit the live FERC site in
   tests), and endpoint tests for `/trends` and `/projects`.
5. The README explains how to run ingestion and start the app locally.

## Explicitly out of scope for v1

No auth/user accounts, no payments, no BLM/Army Corps/EPA data, no §1402
statistical pattern-detection (that's v1.5), no transmission-siting resource
overlay, no geospatial/parcel-level tooling, no production deployment beyond
what's already set up (local dev + the mini worker).
