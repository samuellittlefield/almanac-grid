"""A §1402-style "pattern of disparate treatment" check on BLM project types.

Reads the local database only (no network, no writes). Prints, per type:

1. The six-year table the bill would have the government produce, for every
   calendar year since 2016: filings, decisions by outcome, the open queue at
   year end, how much of it is past deadline, and the 5-year baselines the
   bill compares 2025 and 2026 against.
2. Filing cohorts: the share of each year's applications decided within 12
   months. A backlog measure lags; this one shows when processing changed.

Definitions (approximations, see docs/plan.md "§1402 and what FERC data
alone can show"):
- Overdue = open more than 60 days past the deadline. Deadline = filing date
  + 2 years for cases with an EIS, + 1 year otherwise (the bill's 1-year rule
  and NEPA §107(g)'s EA limit). The bill counts from a *complete*
  application; BLM publishes only the date an application arrived, so delay
  is overstated.
- Denial rate = denied / (approved + denied + withdrawn). "outcome_unclear"
  counts for timing only, as in the app.
- Baseline for year Y = the 5 previous year-end snapshots pooled.
- Rows with a decision date in the future, or before the filing date, are
  dropped (an ingest bug: some MLRS grants carry their term end in the
  decision-date field).
- The last year runs to the data's as-of date: when the MLRS case cache was
  last refreshed.

Run:
    python -m analysis.s1402_blm
    python -m analysis.s1402_blm --json results.json
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
from datetime import date, timedelta

from sqlalchemy import text

from app.database import engine

FIRST_YEAR = 2016
DECIDED = ("approved", "denied", "withdrawn")
GROUPS = {
    "solar": "p.project_type = 'solar'",
    "onshore_wind": "p.project_type = 'onshore_wind'",
    "geothermal": "p.project_type = 'geothermal'",
    "transmission (EA/EIS)": "p.project_type = 'electric_transmission' and a.is_major",
    "oil/gas pipeline (EA/EIS)": "p.project_type = 'oil_gas_pipeline' and a.is_major",
    "transmission (all)": "p.project_type = 'electric_transmission'",
}


def data_as_of(conn) -> date:
    """When the BLM case data was last fetched."""
    fetched = conn.execute(text("select max(fetched_at) from blm_case_cache")).scalar()
    return fetched.date() if fetched else date.today()


def load(conn, where: str, as_of: date) -> tuple[list, int]:
    rows = conn.execute(
        text(
            f"""select a.docket_number, a.filed_date, a.decision_date, a.outcome, a.review_tier
            from agency_action a join project p on p.id = a.project_id
            where a.agency = 'BLM' and {where}"""
        )
    ).all()
    good = [
        r
        for r in rows
        if not (r.decision_date and (r.decision_date > as_of or (r.filed_date and r.decision_date < r.filed_date)))
    ]
    return good, len(rows) - len(good)


def add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # Feb 29
        return date(day.year + years, 2, 28)


def deadline(filed: date, tier: str | None) -> date:
    return add_years(filed, 2 if tier == "eis" else 1)


def year_table(rows, as_of: date) -> dict[int, dict]:
    table = {}
    for year in range(FIRST_YEAR, as_of.year + 1):
        end = min(date(year, 12, 31), as_of)
        decided = [r for r in rows if r.outcome != "pending" and r.decision_date and r.decision_date.year == year]
        counts = collections.Counter(r.outcome for r in decided)
        open_queue = [
            r
            for r in rows
            if r.filed_date
            and r.filed_date <= end
            and (r.outcome == "pending" or (r.decision_date and r.decision_date > end))
        ]
        overdue = [r for r in open_queue if end > deadline(r.filed_date, r.review_tier) + timedelta(days=60)]
        grant_days = [(r.decision_date - r.filed_date).days for r in decided if r.outcome == "approved" and r.filed_date]
        table[year] = {
            "filed": sum(1 for r in rows if r.filed_date and r.filed_date.year == year),
            "approved": counts["approved"],
            "denied": counts["denied"],
            "withdrawn": counts["withdrawn"],
            "outcome_unclear": counts["outcome_unclear"],
            "decided": sum(counts[o] for o in DECIDED),
            "open_at_year_end": len(open_queue),
            "overdue_at_year_end": len(overdue),
            "median_days_to_grant": statistics.median(grant_days) if grant_days else None,
        }
    return table


def cohorts(rows, as_of: date) -> dict[int, dict]:
    """Share of each filing year's applications decided within 12 months."""
    out = {}
    for year in range(FIRST_YEAR, as_of.year):
        cohort = [r for r in rows if r.filed_date and r.filed_date.year == year]
        within = sum(
            1
            for r in cohort
            if r.outcome != "pending" and r.decision_date and (r.decision_date - r.filed_date).days <= 365
        )
        out[year] = {"filed": len(cohort), "decided_within_12_months": within}
    return out


def baseline(table: dict[int, dict], year: int, num: str, den: str) -> tuple[int, int]:
    prev = [table[y] for y in range(year - 5, year) if y in table]
    return sum(x[num] for x in prev), sum(x[den] for x in prev)


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:5.1f}%" if d else "   — "


def report(label: str, rows, dropped: int, table: dict, cohort: dict, as_of: date) -> None:
    undated = sum(1 for r in rows if not r.filed_date)
    print(f"\n=== {label}  ({len(rows)} cases; {dropped} dropped for impossible dates; {undated} without a filing date) ===")
    print(" year   filed  appr  deny  wdrn  uncl | deny rate | open  overdue  share | median days to grant")
    for year, x in table.items():
        mark = "*" if year == as_of.year else " "
        print(
            f" {year}{mark} {x['filed']:5d} {x['approved']:5d} {x['denied']:5d} {x['withdrawn']:5d} "
            f"{x['outcome_unclear']:5d} |  {pct(x['denied'], x['decided'])}  | {x['open_at_year_end']:4d} "
            f"{x['overdue_at_year_end']:7d} {pct(x['overdue_at_year_end'], x['open_at_year_end'])} | "
            f"{x['median_days_to_grant'] if x['median_days_to_grant'] is not None else '—'}"
        )
    for year in (as_of.year - 1, as_of.year):
        x = table[year]
        dn, dd = baseline(table, year, "denied", "decided")
        on, od = baseline(table, year, "overdue_at_year_end", "open_at_year_end")
        print(
            f"  {year} vs {year - 5}–{year - 1}: denial {pct(x['denied'], x['decided'])} vs {pct(dn, dd)} "
            f"(n={x['decided']} vs {dd}); overdue {pct(x['overdue_at_year_end'], x['open_at_year_end'])} vs {pct(on, od)}"
        )
    print("  decided within 12 months, by year filed:")
    print("   " + "  ".join(f"{y}: {pct(c['decided_within_12_months'], c['filed']).strip()} (n={c['filed']})" for y, c in cohort.items()))
    print(f"   ({as_of.year - 1} filings made late in the year have not had a full 12 months)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", metavar="PATH", help="Also write the tables as JSON.")
    args = parser.parse_args(argv)

    results = {}
    with engine.connect() as conn:
        as_of = data_as_of(conn)
        print(f"BLM data as of {as_of}; {as_of.year} is partial.")
        for label, where in GROUPS.items():
            rows, dropped = load(conn, where, as_of)
            table, cohort = year_table(rows, as_of), cohorts(rows, as_of)
            report(label, rows, dropped, table, cohort, as_of)
            results[label] = {"by_year": table, "filing_cohorts": cohort}

    if args.json:
        with open(args.json, "w") as out:
            json.dump({"as_of": as_of.isoformat(), "types": results}, out, indent=1, default=str)
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
