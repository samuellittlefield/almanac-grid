"""Audit where BLM `is_major` flags come from.

`is_major` means some ePlanning EA or EIS names the case's serial. Two
routes can attach a review that isn't really the case's own:

1. Segment suffixes are dropped when joining (scripts/eplanning.py
   `serial_keys`), so a segment such as AZA 034187-02 inherits the tier of a
   project filed under AZA 034187. Often right (phases of one project), but a
   side right-of-way (a tortoise fence on a solar farm's serial) is flagged
   too.
2. A case takes the highest tier of every review naming its serial, so a
   later EA (an amendment, an exploration project, a maintenance permit) can
   mark an earlier decision as major.

For every major BLM action this counts which route its flag came by. It
re-reads BLM's GIS case layers (public, a few minutes) for the legacy
serials, which the database doesn't store; the database is only read.

Run:
    python -m analysis.major_flag_audit
"""

from __future__ import annotations

import collections
import re
from datetime import date

from sqlalchemy import text

from app.database import SessionLocal
from app.models import BlmEplanningCache
from scripts.eplanning import review_tier, serial_keys
from scripts.ingest_blm import _build_http_session, fetch_cases

START = date(2016, 1, 1)
# Like scripts.eplanning._SERIAL_RE, but keeps the segment suffix.
_STRICT_RE = re.compile(r"\b([A-Z]{1,6})[\s_-]*(\d{1,9})(?:([A-Z]{1,2})\b|[\s-](\d{2})\b)?")


def strict_keys(value: str | None) -> set[tuple[str, str | None]]:
    """(serial key, segment or None) for every serial in `value`."""
    keys = set()
    for letters, digits, letter_seg, num_seg in _STRICT_RE.findall((value or "").upper()):
        segment = letter_seg or num_seg or None
        if len(digits) >= 8 and digits.startswith("0"):
            digits, segment = digits[:-2], digits[-2:]
        number = digits.lstrip("0") or "0"
        for prefix in [letters] + (["NVN"] if letters == "N" else []):
            keys.add((prefix + number, segment))
    return keys


def _date(value: str | None) -> date | None:
    try:
        month, day, year = (int(part) for part in (value or "").split("/"))
        return date(year, month, day)
    except ValueError:
        return None


def main() -> None:
    db = SessionLocal()
    try:
        major = {
            row.docket_number: row
            for row in db.execute(
                text(
                    "select a.docket_number, a.review_tier, a.decision_date, a.outcome, p.project_type, p.name "
                    "from agency_action a join project p on p.id = a.project_id "
                    "where a.agency = 'BLM' and a.is_major"
                )
            )
        }
        projects_by_key = collections.defaultdict(list)
        for entry in db.query(BlmEplanningCache):
            payload = entry.payload or {}
            tier = review_tier(payload.get("Project Type") or payload.get("_listing_type"))
            if tier not in ("ea", "eis"):
                continue
            info = {
                "id": entry.project_id,
                "tier": tier,
                "name": payload.get("Project Name") or "",
                "case_file": payload.get("Case File Number") or "",
                "start": _date(payload.get("Start Date")),
                "strict": strict_keys(payload.get("Case File Number")),
            }
            for key in serial_keys(payload.get("Case File Number")):
                projects_by_key[key].append(info)
    finally:
        db.close()

    print(f"{len(major)} major BLM actions. Re-reading BLM's case layers...", flush=True)
    cases = {case.cse_nr: case for case in fetch_cases(_build_http_session(), START, log=lambda _: None)}

    route = collections.defaultdict(collections.Counter)
    timing = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    for cse_nr, row in major.items():
        case = cases.get(cse_nr)
        if case is None:
            route[row.project_type]["not in the layers now"] += 1
            continue
        matches = [p for key in case.serial_keys for p in projects_by_key.get(key, []) if p["tier"] == row.review_tier]
        own = strict_keys(case.legacy_serial) | strict_keys(case.cse_nr)
        if not any(segment for _, segment in own):
            kind = "own serial, no segment"
        elif any(own & p["strict"] for p in matches):
            kind = "segment named exactly"
        else:
            kind = "segment inherits the base serial's review"
            if len(examples[row.project_type]) < 5:
                examples[row.project_type].append(f"{row.name[:45]!r} ({case.legacy_serial}) <- {matches[0]['name'][:50]!r}")
        route[row.project_type][kind] += 1

        starts = [p["start"] for p in matches if p["start"]]
        if not starts:
            when = "review has no start date"
        elif row.outcome == "pending":
            when = "still pending"
        elif min(starts) <= row.decision_date:
            when = "review started before the decision"
        else:
            when = "every review started after the decision"
        timing[row.project_type][when] += 1

    print("\nHow each flag was matched:")
    for project_type, counts in sorted(route.items()):
        print(f"  {project_type}: {dict(counts)}")
    print("\nExamples of inherited reviews:")
    for project_type, lines in examples.items():
        for line in lines:
            print(f"  [{project_type}] {line}")
    print("\nDid the review start before the decision it flags?")
    for project_type, counts in sorted(timing.items()):
        print(f"  {project_type}: {dict(counts)}")


if __name__ == "__main__":
    main()
