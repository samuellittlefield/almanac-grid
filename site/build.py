"""Build the public Almanac Grid page from a frozen snapshot (no database).

Reads snapshots/<date>/ (see "Public page" in README.md): checks every
file against SHA256SUMS, then computes the page's figures from
s1402_blm.json and the project / agency_action CSVs, and writes
site/dist/index.html plus site/dist/figures.json (every number the page shows).

Run:
    python site/build.py                        # newest snapshot
    python site/build.py --snapshot snapshots/2026-11-19 --linkedin https://...
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parent.parent
SITE = Path(__file__).resolve().parent
FIRST_YEAR = 2016
TIMING_LAST_FILING_YEAR = 2023  # every filing through 2023 has had > 34 months

# Same groups, in the same order, as analysis/s1402_blm.py GROUPS.
GROUPS = [
    ("solar", "Solar", lambda p, a: p == "solar"),
    ("onshore_wind", "Onshore wind", lambda p, a: p == "onshore_wind"),
    ("geothermal", "Geothermal leases", lambda p, a: p == "geothermal"),
    ("transmission (EA/EIS)", "Electric transmission, EA/EIS reviews", lambda p, a: p == "electric_transmission" and a["is_major"] == "t"),
    ("oil/gas pipeline (EA/EIS)", "Oil and gas pipelines, EA/EIS reviews", lambda p, a: p == "oil_gas_pipeline" and a["is_major"] == "t"),
    ("transmission (all)", "All power-line rights-of-way", lambda p, a: p == "electric_transmission"),
]
GROUP_NOTES = {
    "onshore_wind": "1 to 5 filings a year: too few to read as a trend.",
    "geothermal": "Lease decisions, which come before (and are not) a power plant's permits.",
    "transmission (all)": "BLM records can't separate transmission from distribution lines; most of these are small lines.",
}


# ---------------------------------------------------------------- snapshot

def verify(snapshot: Path) -> dict[str, str]:
    sums = {}
    for line in (snapshot / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        actual = hashlib.sha256((snapshot / name).read_bytes()).hexdigest()
        if actual != digest:
            sys.exit(f"{name}: checksum mismatch; the snapshot has changed since SHA256SUMS was written")
        sums[name] = digest
    return sums


def to_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def load_rows(snapshot: Path, as_of: date) -> dict[str, dict]:
    """BLM agency_action rows per group, with analysis/s1402_blm.py's date filter."""
    types = {}
    with open(snapshot / "project.csv", newline="") as f:
        for p in csv.DictReader(f):
            types[p["id"]] = p["project_type"]
    groups = {key: {"rows": [], "dropped": 0} for key, _, _ in GROUPS}
    with open(snapshot / "agency_action.csv", newline="") as f:
        for a in csv.DictReader(f):
            if a["agency"] != "BLM":
                continue
            ptype = types[a["project_id"]]
            filed, decided = to_date(a["filed_date"]), to_date(a["decision_date"])
            row = {"filed": filed, "decided": decided, "outcome": a["outcome"], "tier": a["review_tier"]}
            bad = decided and (decided > as_of or (filed and decided < filed))
            for key, _, match in GROUPS:
                if match(ptype, a):
                    if bad:
                        groups[key]["dropped"] += 1
                    else:
                        groups[key]["rows"].append(row)
    return groups


# ---------------------------------------------------------------- figures

def share(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def timing(rows: list[dict], as_of: date) -> dict:
    """Filings 2016 through TIMING_LAST_FILING_YEAR: how long they took."""
    cohort = [r for r in rows if r["filed"] and FIRST_YEAR <= r["filed"].year <= TIMING_LAST_FILING_YEAR]
    done = [r for r in cohort if r["outcome"] != "pending" and r["decided"]]
    days = [(r["decided"] - r["filed"]).days for r in done]
    within = lambda limit: sum(1 for d in days if d <= limit)  # noqa: E731
    pending = sum(1 for r in cohort if r["outcome"] == "pending")
    quart = statistics.quantiles(days, n=4) if len(days) >= 4 else None
    return {
        "filed": len(cohort),
        "within_12_months_pct": share(within(365), len(cohort)),
        "within_24_months_pct": share(within(730), len(cohort)),
        "pending_pct": share(pending, len(cohort)),
        "pending": pending,
        "decided": len(done),
        "median_months_to_decision": round(statistics.median(days) / 30.44, 1) if days else None,
        "quartile_months": [round(q / 30.44, 1) for q in (quart[0], quart[2])] if quart else None,
    }


def outcomes(by_year: dict) -> dict:
    """How decisions dated 2016 onward ended (from the §1402 table)."""
    tot = {k: sum(y[k] for y in by_year.values()) for k in ("approved", "denied", "withdrawn", "outcome_unclear")}
    n = sum(tot.values())
    return {"decisions": n, **{k: v for k, v in tot.items()}, **{f"{k}_pct": share(v, n) for k, v in tot.items()}}


def baseline_series(by_year: dict) -> dict[int, float | None]:
    """The introduced text's lookback: the previous 5 year ends, pooled, once all 5 exist."""
    out = {}
    for year in by_year:
        prev = [by_year[y] for y in range(year - 5, year) if y in by_year]
        if len(prev) == 5:
            out[year] = share(sum(p["overdue_at_year_end"] for p in prev), sum(p["open_at_year_end"] for p in prev))
    return out


def baseline_10_series(by_year: dict) -> dict[int, dict]:
    """The lookback reportedly in the Senate-passed text: the previous 10 year ends, pooled.

    The data starts in FIRST_YEAR, so each year uses whichever of its previous 10 year ends
    exist (only the last year has all 10). Shown for the same years as the 5-year baseline.
    """
    out = {}
    for year in baseline_series(by_year):
        prev = [y for y in range(year - 10, year) if y in by_year and y >= FIRST_YEAR]
        out[year] = {
            "pct": share(sum(by_year[y]["overdue_at_year_end"] for y in prev), sum(by_year[y]["open_at_year_end"] for y in prev)),
            "first": min(prev),
            "last": max(prev),
            "years": len(prev),
        }
    return out


def compute(snapshot: Path) -> dict:
    s1402 = json.loads((snapshot / "s1402_blm.json").read_text())
    as_of = date.fromisoformat(s1402["as_of"])
    groups = load_rows(snapshot, as_of)
    out = {"as_of": s1402["as_of"], "snapshot": snapshot.name, "groups": []}
    for key, label, _ in GROUPS:
        t = s1402["types"][key]
        by_year = {int(y): v for y, v in t["by_year"].items()}
        cohorts = {int(y): v for y, v in t["filing_cohorts"].items()}
        rows = groups[key]["rows"]
        last_cohort = max(cohorts)
        cutoff = date(as_of.year - 1, as_of.month, as_of.day)
        out["groups"].append({
            "key": key,
            "label": label,
            "note": GROUP_NOTES.get(key),
            "cases": len(rows),
            "dropped_impossible_dates": groups[key]["dropped"],
            "without_filing_date": sum(1 for r in rows if not r["filed"]),
            "with_review_tier": sum(1 for r in rows if r["tier"]),
            "cohorts": [
                {"year": y, "filed": c["filed"], "within": c["decided_within_12_months"],
                 "pct": share(c["decided_within_12_months"], c["filed"])}
                for y, c in sorted(cohorts.items())
            ],
            # filings in the last cohort year that haven't yet had 12 months
            "last_cohort_short": sum(1 for r in rows if r["filed"] and r["filed"].year == last_cohort and r["filed"] > cutoff),
            "overdue": [
                {"year": y, "open": v["open_at_year_end"], "overdue": v["overdue_at_year_end"],
                 "pct": share(v["overdue_at_year_end"], v["open_at_year_end"])}
                for y, v in sorted(by_year.items())
            ],
            "baseline": baseline_series(by_year),
            "baseline_10": baseline_10_series(by_year),
            "timing": timing(rows, as_of),
            "outcomes": outcomes(by_year),
        })
    return out


STATUS_AGE_MONTHS = 36
STATUS_PERIODS = [("2016–19", 2016, 2019), ("2020–22", 2020, 2022), ("2023", 2023, 2023)]
STATUS_OUTCOMES = ["approved", "denied", "withdrawn", "outcome_unclear", "pending"]


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


def solar_status_at_36(snapshot: Path, as_of: date) -> dict:
    """Every BLM solar application, as it stood 36 months after filing.

    Unlike the other figures, this keeps records whose decision is dated before
    the filing date: a decision on or before the 36-month mark counts, whatever
    its date. Only applications at least 36 months old on as_of qualify.
    """
    solar_projects = set()
    with open(snapshot / "project.csv", newline="") as f:
        for p in csv.DictReader(f):
            if p["project_type"] == "solar":
                solar_projects.add(p["id"])
    rows = []
    with open(snapshot / "agency_action.csv", newline="") as f:
        for a in csv.DictReader(f):
            if a["agency"] == "BLM" and a["project_id"] in solar_projects:
                rows.append({"serial": a["docket_number"], "filed": to_date(a["filed_date"]),
                             "decided": to_date(a["decision_date"]), "outcome": a["outcome"]})
    periods = []
    for label, first, last in STATUS_PERIODS:
        filed = [r for r in rows if r["filed"] and first <= r["filed"].year <= last]
        old_enough = [r for r in filed if add_months(r["filed"], STATUS_AGE_MONTHS) <= as_of]
        counts = dict.fromkeys(STATUS_OUTCOMES, 0)
        for r in old_enough:
            mark = add_months(r["filed"], STATUS_AGE_MONTHS)
            decided = r["outcome"] != "pending" and r["decided"] and r["decided"] <= mark
            counts[r["outcome"] if decided else "pending"] += 1
        n = len(old_enough)
        periods.append({
            "label": label, "first": first, "last": last,
            "applications": n, "filed_in_period": len(filed), "too_young": len(filed) - n,
            **counts, **{f"{k}_pct": share(v, n) for k, v in counts.items()},
        })
    cutoff = add_months(as_of, -STATUS_AGE_MONTHS)
    return {
        "age_months": STATUS_AGE_MONTHS,
        "filed_on_or_before": cutoff.isoformat(),
        "periods": periods,
        "decided_before_filing": sorted(r["serial"] for r in rows if r["filed"] and r["decided"] and r["decided"] < r["filed"]),
        "later_filings_not_shown": sum(1 for r in rows if r["filed"] and r["filed"] > cutoff),
        "without_filing_date": sum(1 for r in rows if not r["filed"]),
    }


def pooled(cohorts: list[dict], first: int, last: int) -> dict:
    sel = [c for c in cohorts if first <= c["year"] <= last]
    within, filed = sum(c["within"] for c in sel), sum(c["filed"] for c in sel)
    return {"first": first, "last": last, "within": within, "filed": filed, "pct": share(within, filed)}


# ---------------------------------------------------------------- chart HTML

def fmt_pct(v: float | None, digits: int = 0) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"


def column_chart(points: list[dict], label: str, *, height: int = 240, show_n: bool = True,
                 sparse_x: bool = False, mark_last: bool = False) -> Markup:
    """Single-series columns on a 0-100% scale, laid out in CSS so text stays legible at any width."""
    ticks = [0, 25, 50, 75, 100] if height >= 200 else [0, 50, 100]
    grid = "".join(f'<div class="grid" style="bottom:{t}%"><span>{t}%</span></div>' for t in ticks)
    cols, xs = [], []
    for i, p in enumerate(points):
        last = mark_last and i == len(points) - 1
        tip = f"Filed {p['year']}: {fmt_pct(p['pct'], 1)} decided within 12 months ({p['within']} of {p['filed']})"
        if last:
            tip += ". Some of these filings have not yet had 12 months."
        bar = f'<div class="bar" style="height:{p["pct"] or 0}%"></div>' if p["pct"] else '<div class="bar zero"></div>'
        cols.append(f'<div class="col" tabindex="0" role="img" aria-label="{escape(tip)}" data-tip="{escape(tip)}">{bar}</div>')
        show = not sparse_x or i in (0, len(points) - 1)
        star = "*" if last else ""
        year = f'<span class="yl">{p["year"]}{star}</span><span class="ys">\u2019{p["year"] % 100:02d}{star}</span>' if show else ""
        n = f'<span class="n"><span class="np">n=</span>{p["filed"]}</span>' if show_n else ""
        xs.append(f'<div class="x">{year}{n}</div>')
    return Markup(
        f'<div class="cchart" style="--plot-h:{height}px" aria-label="{escape(label)}">'
        f'<div class="plot">{grid}<div class="cols">{"".join(cols)}</div></div>'
        f'<div class="xaxis">{"".join(xs)}</div></div>'
    )


def line_chart(group: dict) -> Markup:
    """Overdue share of the open queue at year end, with the 5- and 10-year pooled baselines."""
    pts = group["overdue"]
    years = [p["year"] for p in pts]
    xpos = lambda y: 100 * (years.index(y) + 0.5) / len(years)  # noqa: E731
    obs = [(xpos(p["year"]), p["pct"]) for p in pts if p["pct"] is not None]
    base = [(xpos(y), v) for y, v in group["baseline"].items() if v is not None]
    base10 = [(xpos(y), b["pct"]) for y, b in group["baseline_10"].items() if b["pct"] is not None]
    poly = lambda pp: " ".join(f"{x:.2f},{100 - v:.2f}" for x, v in pp)  # noqa: E731
    grid = "".join(f'<div class="grid" style="bottom:{t}%"><span>{t}%</span></div>' for t in (0, 25, 50, 75, 100))
    svg = (
        '<svg class="lines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline class="s3" points="{poly(base10)}"/><polyline class="s2" points="{poly(base)}"/>'
        f'<polyline class="s1" points="{poly(obs)}"/></svg>'
    )
    dots = []
    for p in pts:
        y = p["year"]
        b = group["baseline"].get(y)
        b10 = group["baseline_10"].get(y)
        when = "Nov 17" if y == years[-1] else "year end"
        tip = f"{y} ({when}): {fmt_pct(p['pct'], 1)} of {p['open']} open applications overdue"
        if b is not None:
            tip += f"; 5-year baseline {fmt_pct(b, 1)} (pooled {y - 5}–{y - 1})"
        if b10 is not None:
            tip += f"; 10-year baseline {fmt_pct(b10['pct'], 1)} (pooled {b10['first']}–{b10['last']}, {b10['years']} years)"
        dots.append(f'<div class="hit" style="left:{xpos(y):.2f}%" tabindex="0" role="img" aria-label="{escape(tip)}" data-tip="{escape(tip)}"></div>')
        dots.append(f'<span class="dot s1" style="left:{xpos(y):.2f}%;bottom:{p["pct"]}%"></span>')
        if b is not None:
            dots.append(f'<span class="dot s2" style="left:{xpos(y):.2f}%;bottom:{b}%"></span>')
        if b10 is not None and b10["pct"] is not None:
            dots.append(f'<span class="dot s3" style="left:{xpos(y):.2f}%;bottom:{b10["pct"]}%"></span>')
    last, lb, lb10 = pts[-1], group["baseline"][years[-1]], group["baseline_10"][years[-1]]["pct"]
    # The two baselines end about a point apart: label the upper one above its dot, the lower one below.
    (hi, hi_name), (lo, lo_name) = sorted([(lb, "5-yr"), (lb10, "10-yr")], reverse=True)
    x_end = f"{xpos(years[-1]):.2f}%"
    labels = (
        f'<span class="endlabel" style="left:{x_end};bottom:{last["pct"]}%">{fmt_pct(last["pct"])}</span>'
        f'<span class="endlabel base" style="left:{x_end};bottom:{hi}%">{hi_name} {fmt_pct(hi)}</span>'
        f'<span class="endlabel base below" style="left:{x_end};bottom:{lo}%">{lo_name} {fmt_pct(lo)}</span>'
    )
    xs = "".join(f'<div class="x">{y}{"*" if y == years[-1] else ""}</div>' for y in years)
    return Markup(
        f'<div class="lchart" style="--plot-h:240px"><div class="plot">{grid}{svg}{"".join(dots)}{labels}</div>'
        f'<div class="xaxis">{xs}</div></div>'
    )


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, help="Snapshot directory (default: newest in snapshots/).")
    parser.add_argument("--out", type=Path, default=SITE / "dist")
    parser.add_argument("--linkedin", default="", help="Byline link. Left empty, the byline links nowhere and is flagged.")
    parser.add_argument("--site-url", default="", help="Public URL, for the canonical link. Optional.")
    args = parser.parse_args(argv)

    snapshot = args.snapshot or max((ROOT / "snapshots").iterdir())
    sums = verify(snapshot)
    fig = compute(snapshot)
    solar = fig["groups"][0]
    fig["solar_lead"] = {
        "early": pooled(solar["cohorts"], 2016, 2020),
        "late": pooled(solar["cohorts"], 2021, 2025),
        "overdue_2025": solar["overdue"][-2] | {"baseline": solar["baseline"][2025], "baseline_10": solar["baseline_10"][2025]},
        "overdue_2026": solar["overdue"][-1] | {"baseline": solar["baseline"][2026], "baseline_10": solar["baseline_10"][2026]},
    }
    fig["solar_status_36_months"] = solar_status_at_36(snapshot, date.fromisoformat(fig["as_of"]))

    env = Environment(loader=FileSystemLoader(SITE), autoescape=select_autoescape(["html"]))
    env.filters["pct"] = fmt_pct
    env.filters["num"] = lambda v: f"{v:,}"
    env.filters["isodate"] = lambda v: date.fromisoformat(v).strftime("%B %-d, %Y")
    html = env.get_template("template.html").render(
        f=fig,
        solar=solar,
        lead=fig["solar_lead"],
        s36=fig["solar_status_36_months"],
        as_of=date.fromisoformat(fig["as_of"]),
        sums=sums,
        linkedin=args.linkedin,
        site_url=args.site_url,
        lead_chart=column_chart(solar["cohorts"], "Solar: share decided within 12 months, by filing year", mark_last=True),
        overdue_chart=line_chart(solar),
        multiples=[(g, column_chart(g["cohorts"], g["label"], height=130, show_n=False, sparse_x=True, mark_last=True)) for g in fig["groups"]],
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(html)
    (args.out / "figures.json").write_text(json.dumps(fig, separators=(",", ":"), default=str))
    print(f"Built {args.out / 'index.html'} from {snapshot} (data as of {fig['as_of']})")
    print(f"  2025 solar filings without a full 12 months yet: {solar['last_cohort_short']} of {solar['cohorts'][-1]['filed']}")
    for p in fig["solar_status_36_months"]["periods"]:
        print(f"  36-month status, filed {p['label']}: {p['applications']} of {p['filed_in_period']} old enough")
    if not args.linkedin:
        print("  No --linkedin given: the byline has no link yet.")


if __name__ == "__main__":
    main()
