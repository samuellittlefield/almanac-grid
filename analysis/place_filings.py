"""Place spike 2: BLM solar filings vs. solar interconnection requests.

Did developers file fewer solar right-of-way applications with BLM relative
to overall solar demand in CA, NV, AZ, NM and UT? By filing year, this sets
the tracker's BLM solar ROW filings against new solar interconnection
requests from LBNL's "Queued Up" data, split by whether a county is mostly
BLM land.

Like analysis.place_solar it writes only its own place_* tables, dropping
and rebuilding them on each run, and reads the tracker's tables.

    python -m analysis.place_fetch --only queued_up counties   # inputs
    python -m analysis.place_filings build
    python -m analysis.place_filings report

Definitions:
- BLM filings: tracker `agency_action` rows for BLM `solar` (Solar
  Development Grant or Lease) whose case states include one of the five,
  counted by filing-date year. Each MLRS serial counts once; one project
  often holds several. Acres are the case record's recorded acres (RCRD_ACRS
  on BLM's case layer). The county is the one holding most of the case's
  polygon area.
- Queue requests: every row of Queued Up's "03. Complete Queue Data" in the
  five states whose type_clean includes Solar (hybrids included), whatever
  its status, by q_year (the year it entered the queue). MW is the solar
  component: the mw_n whose type_n is Solar. The county comes from fips_code,
  falling back to the county name. Queued Up lists only the first county of a
  multi-county request.
- County BLM share: the share of a county's area (Census 1:500k cartographic
  boundaries, clipped to the shoreline) covered by BLM in the SMA layer. It is
  computed in EPSG:5070.
- "BLM-heavy" counties have a BLM share of at least 30%. The thresholds 20%
  and 50% are kept for sensitivity.
- Years run from 2016 to the last year both sources cover in full
  (QUEUE_LAST_YEAR). Later BLM filings are shown but kept out of the ratios.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from sqlalchemy import text

from analysis.place_solar import AREA_CRS, STATES, latest_data_dir, load_sma, sha256
from app.database import engine

FIRST_YEAR = 2016
QUEUE_LAST_YEAR = 2025  # Queued Up 2026 Edition: requests through end of 2025
THRESHOLDS = (20, 30, 50)
MAIN_THRESHOLD = 30
PERIODS = (("2016–19", 2016, 2019), ("2020–22", 2020, 2022), (f"2023–{QUEUE_LAST_YEAR % 100:02d}", 2023, QUEUE_LAST_YEAR))
STATE_FIPS = {"CA": "06", "NV": "32", "AZ": "04", "NM": "35", "UT": "49"}
TABLES = ("place_filings_source", "place_county_land", "place_blm_solar_filing", "place_queue_solar_request", "place_filings_vs_queue")


# --------------------------------------------------------------------------
# Inputs


def load_counties(path: Path) -> gpd.GeoDataFrame:
    counties = gpd.read_file(path)
    counties = counties[counties["STUSPS"].isin(STATES)][["GEOID", "STUSPS", "NAME", "ALAND", "geometry"]]
    return counties.rename(columns={"GEOID": "geoid", "STUSPS": "state", "NAME": "county"}).to_crs(AREA_CRS).reset_index(drop=True)


def county_land(counties: gpd.GeoDataFrame, sma: gpd.GeoDataFrame) -> pd.DataFrame:
    """BLM and all-federal share of each county's area."""
    tree = shapely.STRtree(sma.geometry.values)
    rows = []
    for c in counties.itertuples():
        geom = c.geometry
        hits = tree.query(geom, predicate="intersects")
        bounds = geom.bounds
        area = {"blm": 0.0, "federal": 0.0}
        for i in hits:
            cat = sma["category"].values[i]
            if cat not in ("blm", "other_federal"):
                continue
            piece = shapely.intersection(geom, shapely.clip_by_rect(sma.geometry.values[i], *bounds)).area
            area["federal"] += piece
            if cat == "blm":
                area["blm"] += piece
        total = geom.area
        rows.append(
            {
                "geoid": c.geoid,
                "state": c.state,
                "county": c.county,
                "area_km2": round(total / 1e6, 1),
                "blm_share": min(area["blm"] / total, 1.0),
                "federal_share": min(area["federal"] / total, 1.0),
            }
        )
    return pd.DataFrame(rows)


def blm_filings(cases_path: Path, counties: gpd.GeoDataFrame) -> pd.DataFrame:
    sql = text(
        """
        select a.docket_number as cse_nr, p.name as case_name, p.applicant as customer, p.region,
               a.filed_date, a.decision_date, a.outcome, a.is_major
        from agency_action a join project p on p.id = a.project_id
        where a.agency = 'BLM' and p.project_type = 'solar'
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn)
    states = df["region"].fillna("").str.split(",").apply(lambda s: [x.strip() for x in s if x.strip() in STATES])
    df = df[states.str.len() > 0].copy()
    df["state"] = states[states.str.len() > 0].str.join(",")
    df["filed_year"] = pd.to_datetime(df["filed_date"]).dt.year.astype("Int64")

    cases = gpd.read_file(cases_path)
    acres = cases.groupby("CSE_NR")["RCRD_ACRS"].max()
    cases = cases[cases.geometry.notna()].to_crs(AREA_CRS).dissolve(by="CSE_NR")
    df["recorded_acres"] = df["cse_nr"].map(acres)
    df["gis_acres"] = df["cse_nr"].map(cases.geometry.area / 4046.856)

    tree = shapely.STRtree(counties.geometry.values)
    geoids, methods = [], []
    for cse_nr in df["cse_nr"]:
        if cse_nr not in cases.index:
            geoids.append(None)
            methods.append("no_case_polygon")
            continue
        geom = cases.at[cse_nr, "geometry"]
        hits = tree.query(geom, predicate="intersects")
        if len(hits) == 0:
            geoids.append(None)
            methods.append("polygon_outside_counties")
            continue
        overlaps = shapely.area(shapely.intersection(counties.geometry.values[hits], geom))
        geoids.append(counties["geoid"].values[hits[int(np.argmax(overlaps))]])
        methods.append("polygon_majority" if len(hits) == 1 else "polygon_majority_multi_county")
    df["geoid"] = geoids
    df["county_method"] = methods
    return df.drop(columns=["region"])


def queue_requests(path: Path, counties: gpd.GeoDataFrame) -> pd.DataFrame:
    q = pd.read_excel(path, sheet_name="03. Complete Queue Data", header=1)
    q = q[q["state"].isin(STATES) & q["type_clean"].astype(str).str.contains("Solar")].copy()

    def solar_mw(row) -> float:
        return sum(float(row[f"mw_{i}"] or 0) for i in (1, 2, 3) if row[f"type_{i}"] == "Solar" and pd.notna(row[f"mw_{i}"]))

    q["solar_mw"] = q.apply(solar_mw, axis=1)
    q["total_mw"] = q[["mw_1", "mw_2", "mw_3"]].apply(pd.to_numeric, errors="coerce").sum(axis=1)

    valid = set(counties["geoid"])
    by_name = {(r.state, r.county.lower()): r.geoid for r in counties.itertuples()}
    geoids, methods = [], []
    for fips, county, state in zip(q["fips_code"], q["county"], q["state"]):
        geoid, method = None, "no_county"
        if pd.notna(fips):
            code = str(int(fips))
            # A few rows run two codes together (e.g. 603106019 = 06031 + 06019); keep the first.
            candidates = [code.zfill(5)] + ([code[:4].zfill(5), code[:5]] if len(code) > 5 else [])
            for cand in candidates:
                if cand in valid and cand[:2] == STATE_FIPS[state]:
                    geoid, method = cand, "fips" if cand == code.zfill(5) else "fips_first_of_two"
                    break
            else:
                method = "fips_unmatched"
        if geoid is None and isinstance(county, str):
            name = re.split(r"[,/;&]| and ", county)[0].strip().lower().removesuffix(" county")
            if (state, name) in by_name:
                geoid, method = by_name[(state, name)], "county_name"
            elif method == "no_county":
                method = "county_name_unmatched"
        geoids.append(geoid)
        methods.append(method)
    q["geoid"] = geoids
    q["county_method"] = methods
    q["q_year"] = pd.to_numeric(q["q_year"], errors="coerce").astype("Int64")
    keep = ["entity", "q_id", "region", "state", "county", "fips_code", "geoid", "county_method", "q_date", "q_year",
            "q_status", "type_clean", "service", "cluster", "project_name", "solar_mw", "total_mw"]  # fmt: skip
    return q[keep].rename(columns={"county": "county_raw", "fips_code": "fips_raw"})


# --------------------------------------------------------------------------
# Aggregation


def summarize(filings: pd.DataFrame, queue: pd.DataFrame, land: pd.DataFrame) -> pd.DataFrame:
    share = land.set_index("geoid")["blm_share"]
    rows = []
    for threshold in THRESHOLDS:

        def group(geoids: pd.Series) -> pd.Series:
            s = geoids.map(share)
            return np.where(s.isna(), "no county", np.where(s >= threshold / 100, "BLM-heavy", "other"))

        f = filings[filings.filed_year.notna() & (filings.filed_year >= FIRST_YEAR)].assign(grp=lambda d: group(d.geoid))
        qq = queue[queue.q_year.notna() & queue.q_year.between(FIRST_YEAR, QUEUE_LAST_YEAR)].assign(grp=lambda d: group(d.geoid))
        spans = [(str(y), "year", y, y) for y in range(FIRST_YEAR, int(max(f.filed_year.max(), QUEUE_LAST_YEAR)) + 1)]
        spans += [(label, "period", a, b) for label, a, b in PERIODS]
        spans += [(f"{FIRST_YEAR}–{QUEUE_LAST_YEAR % 100:02d}", "all", FIRST_YEAR, QUEUE_LAST_YEAR)]
        for label, kind, a, b in spans:
            fs = f[f.filed_year.between(a, b)]
            qs = qq[qq.q_year.between(a, b)]
            for grp in ("BLM-heavy", "other", "no county", "all"):
                fg = fs if grp == "all" else fs[fs.grp == grp]
                qg = qs if grp == "all" else qs[qs.grp == grp]
                in_queue_years = b <= QUEUE_LAST_YEAR
                rows.append(
                    {
                        "threshold_pct": threshold,
                        "county_group": grp,
                        "span": label,
                        "span_kind": kind,
                        "first_year": a,
                        "last_year": b,
                        "blm_filings": len(fg),
                        "blm_recorded_acres": float(fg.recorded_acres.fillna(0).sum()),
                        "queue_requests": len(qg) if in_queue_years else None,
                        "queue_solar_mw": float(qg.solar_mw.sum()) if in_queue_years else None,
                        "filings_per_100_requests": (100 * len(fg) / len(qg)) if in_queue_years and len(qg) else None,
                        "acres_per_queue_gw": (fg.recorded_acres.fillna(0).sum() / (qg.solar_mw.sum() / 1000))
                        if in_queue_years and qg.solar_mw.sum()
                        else None,
                    }
                )
    return pd.DataFrame(rows)


def build(data_dir: Path) -> None:
    manifest = json.loads((data_dir / "sources.json").read_text())
    print("Counties and SMA ...", flush=True)
    counties = load_counties(next((data_dir / "counties" / "extracted").glob("*.shp")))
    sma = load_sma(next((data_dir / "sma" / "extracted").glob("*.gdb")))
    land = county_land(counties, sma)
    print("BLM filings ...", flush=True)
    filings = blm_filings(Path(manifest["blm_solar_cases"]["file"]), counties)
    print("Queue requests ...", flush=True)
    queue = queue_requests(Path(manifest["queued_up"]["file"]), counties)
    summary = summarize(filings, queue, land)
    sources = []
    for key in ("queued_up", "counties", "sma", "blm_solar_cases"):
        entry = manifest[key]
        sources.append(
            {
                "source": key,
                "version": entry.get("edition") or entry.get("item_modified") or entry.get("last_modified"),
                "url": entry["url"],
                "file": entry["file"],
                "sha256": sha256(Path(entry["file"])),
                "last_modified": entry.get("last_modified"),
                "downloaded_at": entry["downloaded_at"],
            }
        )
    sources.append(
        {"source": "tracker", "version": "BLM case data as of MLRS cache refresh", "url": None, "file": None,
         "sha256": None, "last_modified": None, "downloaded_at": str(data_as_of())}
    )  # fmt: skip
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"drop table if exists {table}"))
        pd.DataFrame(sources).to_sql("place_filings_source", conn, index=False)
        land.to_sql("place_county_land", conn, index=False)
        filings.to_sql("place_blm_solar_filing", conn, index=False)
        queue.to_sql("place_queue_solar_request", conn, index=False)
        summary.to_sql("place_filings_vs_queue", conn, index=False)
        conn.execute(text("alter table place_county_land add primary key (geoid)"))
    print("Wrote", ", ".join(TABLES))


def data_as_of():
    with engine.connect() as conn:
        return conn.execute(text("select max(fetched_at) from blm_case_cache")).scalar()


# --------------------------------------------------------------------------
# Report


def _n(x) -> str:
    return "–" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.0f}"


def _r(x) -> str:
    return "–" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.1f}"


def report() -> str:
    with engine.connect() as conn:
        s = pd.read_sql("select * from place_filings_vs_queue", conn)
        land = pd.read_sql("select * from place_county_land", conn)
        filings = pd.read_sql("select * from place_blm_solar_filing", conn)
        queue = pd.read_sql("select * from place_queue_solar_request", conn)
    out: list[str] = []
    w = out.append

    def table(threshold: int, kinds: tuple[str, ...]) -> None:
        sub = s[(s.threshold_pct == threshold) & s.span_kind.isin(kinds)]
        w("| Span | BLM-heavy: filings | requests | solar GW | filings per 100 req. | Other: filings | requests | solar GW | filings per 100 req. | All: filings per 100 req. |")
        w("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
        for span in dict.fromkeys(sub.span):
            r = sub[sub.span == span].set_index("county_group")
            h, o, a = r.loc["BLM-heavy"], r.loc["other"], r.loc["all"]
            gw = lambda x: "–" if pd.isna(x) else f"{x / 1000:,.1f}"  # noqa: E731
            w(
                f"| {span} | {_n(h.blm_filings)} | {_n(h.queue_requests)} | {gw(h.queue_solar_mw)} | {_r(h.filings_per_100_requests)} | "
                f"{_n(o.blm_filings)} | {_n(o.queue_requests)} | {gw(o.queue_solar_mw)} | {_r(o.filings_per_100_requests)} | {_r(a.filings_per_100_requests)} |"
            )

    w(f"### By year, BLM-heavy = at least {MAIN_THRESHOLD}% BLM land")
    w("")
    table(MAIN_THRESHOLD, ("year",))
    w("")
    w("### By period")
    w("")
    for t in THRESHOLDS:
        heavy = land[land.blm_share >= t / 100]
        w(f"**Threshold {t}%** ({len(heavy)} BLM-heavy counties of {len(land)}):")
        w("")
        table(t, ("period", "all"))
        w("")
    w(f"### BLM-heavy counties at {MAIN_THRESHOLD}%")
    w("")
    w(", ".join(f"{r.county} {r.state} ({100 * r.blm_share:.0f}%)" for r in land[land.blm_share >= MAIN_THRESHOLD / 100].sort_values("blm_share", ascending=False).itertuples()))
    w("")
    w("### Checks")
    w("")
    sw = queue[queue.q_year.between(FIRST_YEAR, QUEUE_LAST_YEAR)]
    nc = sw[sw.geoid.isna()]
    w(f"Queue requests {FIRST_YEAR}–{QUEUE_LAST_YEAR} with no county: {len(nc)} of {len(sw)}, {nc.solar_mw.sum():,.0f} of {sw.solar_mw.sum():,.0f} solar MW ({100 * nc.solar_mw.sum() / sw.solar_mw.sum():.1f}%).")
    w("")
    w("| Entity | Requests | Solar MW | Years |")
    w("|---|--:|--:|---|")
    for ent, g in nc.groupby("entity"):
        w(f"| {ent} | {len(g)} | {g.solar_mw.sum():,.0f} | {', '.join(str(y) for y in sorted(g.q_year.dropna().unique()))} |")
    w("")
    w("County match methods (all years): " + ", ".join(f"{k} {v}" for k, v in queue.county_method.value_counts().items()))
    w("")
    f = filings[filings.filed_year.isna() | (filings.filed_year >= FIRST_YEAR)]
    w(
        "BLM filings without a filing date (not counted): "
        f"{filings.filed_year.isna().sum()}. County methods for counted filings: "
        + ", ".join(f"{k} {v}" for k, v in f[f.filed_year.notna()].county_method.value_counts().items())
    )
    w("")
    act = queue[queue.q_status == "active"].groupby("state").solar_mw.sum() / 1000
    add = queue[(queue.q_status == "active") & (queue.q_year == QUEUE_LAST_YEAR)].groupby("state").solar_mw.sum() / 1000
    w("Our active solar GW vs LBNL '11. Active Cap. Maps' (see report).")
    w("")
    w("| State | Active solar GW (ours) | Active, entered 2025 (ours) |")
    w("|---|--:|--:|")
    for st in STATES:
        w(f"| {st} | {act.get(st, 0):.2f} | {add.get(st, 0):.2f} |")
    w(f"| Five states | {act.sum():.2f} | {add.sum():.2f} |")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "report"])
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args.data_dir or latest_data_dir())
    else:
        print(report())


if __name__ == "__main__":
    main()
