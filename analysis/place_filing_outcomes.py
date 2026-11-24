"""Outcomes of BLM solar ROW filings in the five Southwest states, by filing
period and county group (Place spike 3).

Reads `place_blm_solar_filing` and `place_county_land` (built by
analysis.place_filings) and prints markdown. It writes nothing.

    python -m analysis.place_filing_outcomes

Definitions:
- Filings: one MLRS serial per row, filed 2016-2025 (rows without a filing
  date are left out), split at the 30% county BLM-share threshold. Filings
  with no mapped case polygon have no county and are shown as their own group.
- Outcomes come from the tracker: approved = granted, denied = rejected,
  withdrawn, outcome_unclear = closed with no reason recorded, pending.
- "At 36 months": the outcome if its decision date is no later than 36
  months after filing, otherwise pending. Only filings whose 36-month mark is
  on or before the data's as-of date (the last MLRS cache refresh) qualify.
- Applicants: the case customer, normalized (case, punctuation, "C/O" and
  "ATTN" tails, corporate suffixes). "Families" go further: they take the
  first distinctive word of the name, after a short alias list, so project
  LLCs like BLYTHE SOLAR II / III / IV collapse together. A family is a rough
  stand-in for a developer: it merges some unrelated firms that share a first
  word and misses parents that file under unrelated project names.
"""

from __future__ import annotations

import re

import pandas as pd
from sqlalchemy import text

from app.database import engine

PERIODS = (("2016–19", 2016, 2019), ("2020–22", 2020, 2022), ("2023–25", 2023, 2025))
THRESHOLD = 0.30
AGE_MONTHS = 36
OUTCOMES = (("approved", "Granted"), ("denied", "Rejected"), ("withdrawn", "Withdrawn"),
            ("outcome_unclear", "Closed, no reason"), ("pending", "Pending"))  # fmt: skip
SUFFIXES = r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LP|LTD|LLP)\b"
GENERIC_FIRST = {"THE", "US", "NEW"}
# Obvious same-developer spellings; kept short on purpose.
FAMILY_ALIASES = {"IP": "INTERSECT", "INTERSECT": "INTERSECT", "NATURGY": "CANDELA", "CANDELA": "CANDELA", "NV": "NV ENERGY",
                  "NEVADA": "NV ENERGY"}  # fmt: skip


def normalize(name: str | None) -> str:
    if not isinstance(name, str) or not name.strip():
        return "UNKNOWN"
    s = name.upper()
    s = re.split(r"\bC/O\b|\bATTN\b|\(", s)[0]
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(SUFFIXES, " ", s)
    return " ".join(s.split()) or "UNKNOWN"


def family(name: str) -> str:
    words = [w for w in name.split() if w not in GENERIC_FIRST]
    if not words:
        return name
    first = words[0]
    if first in FAMILY_ALIASES:
        return FAMILY_ALIASES[first]
    # "174 POWER GLOBAL": keep a leading number with the word after it.
    return " ".join(words[:2]) if first.isdigit() and len(words) > 1 else first


def load() -> tuple[pd.DataFrame, pd.Timestamp]:
    with engine.connect() as conn:
        f = pd.read_sql(
            "select f.*, l.blm_share from place_blm_solar_filing f left join place_county_land l using (geoid)", conn
        )
        as_of = pd.Timestamp(conn.execute(text("select max(fetched_at) from blm_case_cache")).scalar()).normalize()
    f = f[f.filed_year.between(2016, 2025)].copy()
    f["filed_date"] = pd.to_datetime(f["filed_date"])
    f["decision_date"] = pd.to_datetime(f["decision_date"])
    f["period"] = pd.cut(f.filed_year, bins=[2015, 2019, 2022, 2025], labels=[p[0] for p in PERIODS]).astype(str)
    f["group"] = "no county"
    f.loc[f.blm_share >= THRESHOLD, "group"] = "BLM-heavy"
    f.loc[f.blm_share < THRESHOLD, "group"] = "other"
    f["age_mark"] = f.filed_date + pd.DateOffset(months=AGE_MONTHS)
    f["qualifies_36m"] = f.age_mark <= as_of
    decided_by_mark = f.decision_date.notna() & (f.decision_date <= f.age_mark)
    f["outcome_36m"] = f.outcome.where(decided_by_mark, "pending")
    f["applicant"] = f.customer.map(normalize)
    f["family"] = f.applicant.map(family)
    return f, as_of


def shares(df: pd.DataFrame, col: str) -> list[str]:
    n = len(df)
    return [f"{100 * (df[col] == key).sum() / n:.0f}%" if n else "–" for key, _ in OUTCOMES]


def report() -> str:
    f, as_of = load()
    out: list[str] = []
    w = out.append
    w(f"Data as of {as_of.date()} (last MLRS cache refresh). 36-month mark qualifies if filed on or before "
      f"{(as_of - pd.DateOffset(months=AGE_MONTHS)).date()}.")  # fmt: skip
    w("")
    heads = " | ".join(label for _, label in OUTCOMES)
    for title, col, only_qualified in (("Current status", "outcome", False), (f"Status {AGE_MONTHS} months after filing", "outcome_36m", True)):
        w(f"### {title}")
        w("")
        w(f"| Group | Period | Filings{' (qualifying / all)' if only_qualified else ''} | {heads} |")
        w("|---|---|--:|" + "--:|" * len(OUTCOMES))
        for grp in ("BLM-heavy", "other", "no county", "all"):
            g = f if grp == "all" else f[f.group == grp]
            for label, _, _ in PERIODS:
                p = g[g.period == label]
                q = p[p.qualifies_36m] if only_qualified else p
                count = f"{len(q)} / {len(p)}" if only_qualified else f"{len(p)}"
                w(f"| {grp} | {label} | {count} | " + " | ".join(shares(q, col)) + " |")
        w("")

    w("### Applicants")
    w("")
    w("| Period | Filings | Distinct applicants | Filings per applicant | Top-5 applicants' share | Distinct families | Top-5 families' share |")
    w("|---|--:|--:|--:|--:|--:|--:|")
    for label, _, _ in PERIODS:
        p = f[f.period == label]
        a = p.applicant.value_counts()
        fam = p.family.value_counts()
        w(
            f"| {label} | {len(p)} | {len(a)} | {len(p) / len(a):.1f} | {100 * a.head(5).sum() / len(p):.0f}% | "
            f"{len(fam)} | {100 * fam.head(5).sum() / len(p):.0f}% |"
        )
    w("")
    for label, _, _ in PERIODS:
        p = f[f.period == label]
        top = p.applicant.value_counts().head(5)
        topf = p.family.value_counts().head(5)
        w(f"- **{label}** top applicants: " + "; ".join(f"{k.title()} ({v})" for k, v in top.items()))
        w(f"  top families: " + "; ".join(f"{k.title()} ({v})" for k, v in topf.items()))
    w("")
    w("### BLM-heavy counties only: applicants")
    w("")
    w("| Period | Filings | Distinct applicants | Top-5 share | Distinct families | Top-5 families' share |")
    w("|---|--:|--:|--:|--:|--:|")
    for label, _, _ in PERIODS:
        p = f[(f.period == label) & (f.group == "BLM-heavy")]
        a, fam = p.applicant.value_counts(), p.family.value_counts()
        w(f"| {label} | {len(p)} | {len(a)} | {100 * a.head(5).sum() / len(p):.0f}% | {len(fam)} | {100 * fam.head(5).sum() / len(p):.0f}% |")
    w("")
    odd = f[f.decision_date < f.filed_date]
    w(f"Decision dated before filing: {len(odd)} ({', '.join(odd.cse_nr)}). Unknown applicant: {(f.applicant == 'UNKNOWN').sum()}.")
    return "\n".join(out)


def main() -> None:
    print(report())


if __name__ == "__main__":
    main()
