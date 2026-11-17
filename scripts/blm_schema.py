"""Additive schema changes for v2 (BLM). There's no Alembic; this runs at the
start of every BLM ingestion and is safe to repeat.

    python -m scripts.blm_schema
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.database import Base

_AGENCY_ACTION_COLUMNS = (
    "ADD COLUMN IF NOT EXISTS review_tier VARCHAR",
    "ADD COLUMN IF NOT EXISTS is_major BOOLEAN NOT NULL DEFAULT false",
)


def migrate(engine: Engine) -> None:
    """Create new tables (blm_case_cache, blm_eplanning_cache) and add the v2
    columns to agency_action. Never drops or rewrites existing data."""
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "postgresql":
        return  # create_all already built the full current schema
    with engine.begin() as conn:
        for clause in _AGENCY_ACTION_COLUMNS:
            conn.execute(text(f"ALTER TABLE agency_action {clause}"))


if __name__ == "__main__":
    from app.database import engine

    migrate(engine)
    print("Schema up to date.")
