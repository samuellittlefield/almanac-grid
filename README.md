# Almanac Grid

A national weather, geological survey, and permitting-trend tracker — a
farmer's-almanac-style view onto meteorological trends, geological survey
data, and land-use/energy permitting activity (zoning, drilling, siting).

Still in early ideation; scope and data sources are being worked out.

## Stack

- Python / FastAPI
- PostgreSQL (SQLAlchemy)

## Local setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload
```

## Tests

```bash
.venv/bin/pytest
```
