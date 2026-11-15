# Almanac Grid

A federal permitting-outcomes tracker: a normalized database of permitting
decisions (approved / denied / withdrawn, and how long each took) by project
type, so trends are visible over time instead of scattered across agency
dockets. v1 covers FERC only. See [docs/plan.md](docs/plan.md) for the why and
[docs/spec.md](docs/spec.md) for the v1 spec.

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
FERC's eLibrary search and takes about eight minutes. It is safe to re-run at
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
| `EL` | `electric_transmission` | Complaints and petitions for declaratory order |

Outcomes are read from the wording of FERC's order titles ("Order Issuing
Certificate", "Order Denying ...") and from withdrawal notices. Things to know
about the result:

- **Outcomes are heuristic.** Procedural orders (rehearing, stays, extensions,
  compliance) are ignored. An order whose title doesn't say which way it went
  ("Order on Complaint") leaves the action `pending`, so `pending` means "no
  recognizable decision yet", not strictly "undecided". This mostly affects EL
  dockets.
- **Not everything on a CP docket is an application.** Blanket-certificate
  prior-notice filings (which take effect without an order) and
  abandonment-only requests are skipped.
- **EL dockets are broader than transmission.** EL is FERC's general electric
  docket, mostly market and rate disputes, and the search results don't say
  what a given complaint is about, so every original EL complaint or petition
  is ingested under `electric_transmission`.
- **Hydro:** preliminary permits, license transfers and amendments are not
  tracked, and an application filed before the window is only included if its
  decision falls inside it. Filing years before the window therefore show only
  the slow cases; filter the dashboard to the window for comparable numbers.
- **`region`** is filled only when a filing description gives a location
  ("in Greene County, Pennsylvania"), which most don't.
- **Rule changes don't remove old rows.** A re-run updates and adds; if you
  change the classification rules in the script, empty the two tables first.
- **Backstop transmission siting** (the new docket type in S.5653) is not
  ingested: the bill is not law and FERC has no such docket yet. The script
  has a marked place to add it as its own docket type.

## Run the app

```bash
.venv/bin/uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/.

| Page | |
|---|---|
| `/` | Trends dashboard: outcomes and time to decision by project type and filing year |
| `/ui/projects` | Project list and search |
| `/ui/projects/{id}` | Project detail with its full action history |

| JSON API | |
|---|---|
| `GET /trends` | Approval / denial / withdrawal rates and average days to decision per `project_type`, with a per-year breakdown. Filters: `start_date`, `end_date` (filing date) |
| `GET /projects` | List and search. Filters: `project_type`, `applicant` (substring), `region`, `start_date`, `end_date`, plus `limit` / `offset` |
| `GET /projects/{id}` | One project and its `agency_action` history |
| `GET /health` | Liveness check |

Interactive API docs are at `/docs`.

## Tests

```bash
.venv/bin/pytest
```

Tests run against an in-memory SQLite database and a saved sample of FERC
search results (`tests/fixtures/`); they never contact FERC or Postgres.
