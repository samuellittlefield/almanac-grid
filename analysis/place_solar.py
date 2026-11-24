"""The "Place" layer, first pass: how much new Southwest solar sits on federal land.

Solar PV plants of 1 MW and up in CA, NV, AZ, NM and UT, from EIA-860M:
operating generators by year online (2016 on) and planned generators by
expected year. Each plant gets a land owner from BLM's Surface Management
Agency (SMA) layer, using its USPVDB array footprint where there is one and
EIA's coordinates otherwise.

Unlike the other analyses this one writes to the database, but only to its
own `place_*` tables, which it drops and rebuilds on every run. It never
touches the tracker's tables (it reads `project` / `agency_action` for the
cross-check).

    python -m analysis.place_fetch            # download inputs (once)
    python -m analysis.place_solar build      # load, assign owners, write tables
    python -m analysis.place_solar report     # print the markdown report

Method:
- Universe: EIA-860M generators with Technology "Solar Photovoltaic" in the
  five states, Operating sheet with Operating Year >= 2016, plus the Planned
  sheet. No PV generator in these states that came online in 2016 or later is
  on the Retired sheet, so that sheet is ignored. Generators are aggregated
  to the plant (Plant ID); a plant is kept if its PV nameplate (every
  operating and planned PV generator, any year) is at least 1 MW. Capacity is
  nameplate MW (AC), counted in the year each generator came (or is to come)
  online, so a phased plant adds capacity in more than one year but has one
  owner.
- Footprints: USPVDB facility polygons (panel-array outlines, not lease or
  right-of-way boundaries), matched on eia_id = Plant ID.
- Ownership: SMA's combined SurfaceManagementAgency layer. Categories: blm
  (DOI/BLM), other_federal (every other federal agency except BIA), tribal
  (DOI/BIA: lands held in trust), state (ST), private (PVT), and the minor
  local (LG), undetermined (UND) and unmapped (no SMA polygon).
  "Federal" means blm + other_federal.
- Footprint plants take the category with the most area (unmapped included)
  and store each category's share of footprint area. Point plants take the
  category of the polygon containing the point.
- Boundary distance (point plants): distance from the point to the nearest
  land that is not in its own category, capped at 500 m (an SMA boundary
  between two polygons of the same category is not an ownership boundary).
- Areas and distances are computed in EPSG:5070 (CONUS Albers equal-area).
"""

from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import re
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import box
from sqlalchemy import text

from app.database import engine

STATES = ("CA", "NV", "AZ", "NM", "UT")
FIRST_YEAR = 2016
MIN_PLANT_MW = 1.0
AREA_CRS = 5070
BOUNDARY_M = 500
CASE_MATCH_M = 1000
FEDERAL = ("blm", "other_federal")
CATEGORIES = ("blm", "other_federal", "state", "tribal", "private", "local", "undetermined", "unmapped")
TABLES = ("place_source", "place_solar_plant", "place_solar_capacity", "place_solar_land_share", "place_blm_case_check")
DATA_ROOT = Path("data/place")


# --------------------------------------------------------------------------
# Inputs


def latest_data_dir() -> Path:
    dirs = sorted(p for p in DATA_ROOT.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))
    if not dirs:
        raise SystemExit("no data/place/<date>/ folder; run python -m analysis.place_fetch first")
    return dirs[-1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_eia(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (generators, plants) for in-scope PV."""
    frames = []
    for sheet, year_col, status in (("Operating", "Operating Year", "operating"), ("Planned", "Planned Operation Year", "planned")):
        df = pd.read_excel(path, sheet_name=sheet, header=2)
        df = df[df["Plant State"].isin(STATES) & (df["Technology"] == "Solar Photovoltaic")].copy()
        df["status"] = status
        df["year"] = pd.to_numeric(df[year_col], errors="coerce").astype("Int64")
        frames.append(df)
    gens = pd.concat(frames, ignore_index=True)
    gens["mw"] = pd.to_numeric(gens["Nameplate Capacity (MW)"], errors="coerce").fillna(0.0)
    plant_mw = gens.groupby("Plant ID")["mw"].sum()
    in_scope = (gens["status"] == "planned") | (gens["year"] >= FIRST_YEAR)
    keep = set(gens.loc[in_scope, "Plant ID"]) & set(plant_mw[plant_mw >= MIN_PLANT_MW].index)
    gens = gens[gens["Plant ID"].isin(keep)].copy()
    scoped = gens[(gens["status"] == "planned") | (gens["year"] >= FIRST_YEAR)]
    first = scoped.sort_values(["year"]).groupby("Plant ID").first()
    plants = pd.DataFrame(
        {
            "plant_id": first.index.astype(int),
            "plant_name": first["Plant Name"].values,
            "entity_name": first["Entity Name"].values,
            "state": first["Plant State"].values,
            "county": first["County"].values,
            "latitude": first["Latitude"].astype(float).values,
            "longitude": first["Longitude"].astype(float).values,
        }
    ).set_index("plant_id", drop=False)
    by_status = scoped.groupby(["Plant ID", "status"])["mw"].sum().unstack(fill_value=0.0)
    plants["operating_mw"] = by_status.get("operating", pd.Series(dtype=float)).reindex(plants.index).fillna(0.0)
    plants["planned_mw"] = by_status.get("planned", pd.Series(dtype=float)).reindex(plants.index).fillna(0.0)
    plants["pre2016_mw"] = (
        gens[(gens["status"] == "operating") & (gens["year"] < FIRST_YEAR)].groupby("Plant ID")["mw"].sum().reindex(plants.index).fillna(0.0)
    )
    plants["first_year"] = scoped.groupby("Plant ID")["year"].min().reindex(plants.index).astype(int)
    capacity = (
        scoped.groupby(["Plant ID", "status", "year"])
        .agg(mw=("mw", "sum"), generators=("Generator ID", "count"), eia_statuses=("Status", lambda s: "; ".join(sorted(set(s)))))
        .reset_index()
        .rename(columns={"Plant ID": "plant_id"})
    )
    return capacity, plants


def load_uspvdb(path: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    gdf = gdf[["case_id", "eia_id", "p_name", "p_state", "p_year", "p_cap_ac", "p_area", "p_dig_conf", "geometry"]]
    return gdf.to_crs(AREA_CRS)


def sma_category(dept: str | None, agency: str | None) -> str:
    if agency == "BLM":
        return "blm"
    if agency == "BIA":
        return "tribal"
    return {"ST": "state", "PVT": "private", "LG": "local", "UND": "undetermined"}.get(dept or "", "other_federal")


def load_sma(gdb: Path) -> gpd.GeoDataFrame:
    """SMA polygons touching the five states, made valid and split into
    single polygons so the spatial index has tight boxes. Cached next to the
    geodatabase (reading and repairing it takes about a minute)."""
    cache = gdb.parent.parent / "sma_sw_5070.pkl"
    if cache.exists() and cache.stat().st_mtime > gdb.stat().st_mtime:
        return pd.read_pickle(cache)
    sma = pyogrio.read_dataframe(
        gdb,
        layer="SurfaceManagementAgency",
        where="ADMIN_ST <> 'AK'",
        columns=["ADMIN_ST", "ADMIN_UNIT_NAME", "ADMIN_DEPT_CODE", "ADMIN_AGENCY_CODE"],
    )
    sw = gpd.GeoSeries([box(-124.6, 31.2, -102.9, 42.1)], crs=4326).to_crs(sma.crs).iloc[0]
    sma = sma[sma.intersects(sw)].copy()
    sma["geometry"] = shapely.make_valid(sma.geometry.values)
    sma = sma.to_crs(AREA_CRS).explode(index_parts=False)
    sma = sma[sma.geom_type == "Polygon"].reset_index(drop=True)
    sma["category"] = [sma_category(d, a) for d, a in zip(sma.ADMIN_DEPT_CODE, sma.ADMIN_AGENCY_CODE)]
    shapely.prepare(sma.geometry.values)
    sma.to_pickle(cache)
    return sma


# --------------------------------------------------------------------------
# Ownership


def footprint_shares(footprints: gpd.GeoDataFrame, sma: gpd.GeoDataFrame) -> pd.DataFrame:
    """Area of each footprint in each SMA category / agency."""
    tree = shapely.STRtree(sma.geometry.values)
    fp_idx, sma_idx = tree.query(footprints.geometry.values, predicate="intersects")
    fp = footprints.geometry.values[fp_idx]
    bounds = shapely.bounds(fp)
    # Clip each (often state-sized) SMA polygon to the footprint's box first.
    parts = sma.geometry.values[sma_idx]
    clipped = np.array([shapely.clip_by_rect(g, *b) for g, b in zip(parts, bounds)], dtype=object)
    areas = shapely.area(shapely.intersection(fp, clipped))
    pieces = pd.DataFrame(
        {
            "plant_id": footprints["plant_id"].values[fp_idx],
            "owner_category": sma["category"].values[sma_idx],
            "agency_code": sma["ADMIN_AGENCY_CODE"].values[sma_idx],
            "area_m2": areas,
        }
    )
    shares = pieces.groupby(["plant_id", "owner_category", "agency_code"], as_index=False)["area_m2"].sum()
    total = footprints.set_index("plant_id").geometry.area
    mapped = shares.groupby("plant_id")["area_m2"].sum()
    gap = (total - mapped.reindex(total.index).fillna(0.0)).clip(lower=0.0)
    gap = gap[gap > 1.0]
    shares = pd.concat(
        [shares, pd.DataFrame({"plant_id": gap.index, "owner_category": "unmapped", "agency_code": None, "area_m2": gap.values})],
        ignore_index=True,
    )
    shares["share"] = shares["area_m2"] / shares["plant_id"].map(total)
    return shares[shares["area_m2"] > 0]


def point_owner(points: gpd.GeoDataFrame, sma: gpd.GeoDataFrame) -> pd.DataFrame:
    """Owner of the polygon containing each point, and how far the point is
    from land in any other category (or with no SMA polygon), up to 500 m."""
    tree = shapely.STRtree(sma.geometry.values)
    rows = []
    geoms = sma.geometry.values
    for plant_id, point in zip(points["plant_id"], points.geometry.values):
        inside = tree.query(point, predicate="intersects")
        cats = sorted(set(sma["category"].values[inside]))
        if len(inside) == 0:
            category, agency, unit = "unmapped", None, None
        else:
            # Overlapping SMA polygons are rare; prefer a federal one, then the first.
            order = sorted(inside, key=lambda i: (sma["category"].values[i] not in FEDERAL, i))
            category = sma["category"].values[order[0]]
            agency = sma["ADMIN_AGENCY_CODE"].values[order[0]]
            unit = sma["ADMIN_UNIT_NAME"].values[order[0]]
        window = shapely.buffer(point, BOUNDARY_M)
        near = tree.query(window, predicate="intersects")
        x, y = point.x, point.y
        local = {i: shapely.clip_by_rect(geoms[i], x - BOUNDARY_M, y - BOUNDARY_M, x + BOUNDARY_M, y + BOUNDARY_M) for i in near}
        same = [local[i] for i in near if sma["category"].values[i] == category]
        other_land = window if category == "unmapped" else shapely.difference(window, shapely.union_all(same)) if same else window
        if category == "unmapped":
            # distance to the nearest mapped land of any category
            mapped = [local[i] for i in near]
            distance = shapely.distance(point, shapely.union_all(mapped)) if mapped else None
        else:
            distance = None if shapely.area(other_land) < 1.0 else shapely.distance(point, other_land)
        rows.append(
            {
                "plant_id": plant_id,
                "owner_category": category,
                "owner_agency": agency,
                "owner_unit": unit,
                "overlapping_categories": ",".join(cats) if len(cats) > 1 else None,
                "boundary_distance_m": None if distance is None else round(float(distance), 1),
            }
        )
    out = pd.DataFrame(rows)
    out["near_boundary_500m"] = out["boundary_distance_m"].notna()
    return out


# --------------------------------------------------------------------------
# Tracker cross-check


def blm_grants() -> pd.DataFrame:
    """Solar ROW cases the tracker records as granted 2016-2023 in the five states."""
    sql = text(
        """
        select a.docket_number as cse_nr, p.name as case_name, p.applicant as customer, p.region,
               a.decision_date, a.is_major
        from agency_action a join project p on p.id = a.project_id
        where a.agency = 'BLM' and p.project_type = 'solar' and a.outcome = 'approved'
          and a.decision_date between '2016-01-01' and '2023-12-31'
        order by a.decision_date, a.docket_number
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn)
    return df[df["region"].fillna("").str.split(",").apply(lambda states: any(s.strip() in STATES for s in states))]


GENERIC_WORDS = {
    "SOLAR", "ENERGY", "LLC", "INC", "PROJECT", "CENTER", "HYBRID", "POWER", "COMPANY", "CORP", "CORPORATION",
    "THE", "AND", "ATTN", "DEVELOPMENT", "RENEWABLE", "RENEWABLES", "HOLDINGS", "ASSET", "FARM", "PHASE",
    "PARTNERS", "GENERATION", "STATION", "FACILITY", "NORTH", "SOUTH", "EAST", "WEST", "CITY", "SITE",
}  # fmt: skip


def name_words(*names) -> set[str]:
    words = set()
    for name in names:
        if isinstance(name, str):
            words |= {w for w in re.findall(r"[A-Z]+", name.upper()) if len(w) >= 3 and w not in GENERIC_WORDS}
    return words


def case_check(grants: pd.DataFrame, cases_path: Path, plants: gpd.GeoDataFrame) -> pd.DataFrame:
    """Match each grant to the plant within 1 km of its case polygon that
    shares a distinctive word with the case's name or customer, else the one
    overlapping it most, else the nearest. `plants` carries plant_id,
    plant_name, entity_name and uspvdb_name."""
    cases = gpd.read_file(cases_path)
    cases = cases[cases.geometry.notna()].to_crs(AREA_CRS)
    case_geom = cases.dissolve(by="CSE_NR")[["geometry", "layer"]]
    tree = shapely.STRtree(plants.geometry.values)
    rows = []
    for grant in grants.itertuples():
        row = {
            "cse_nr": grant.cse_nr,
            "case_name": grant.case_name,
            "customer": grant.customer,
            "state": grant.region,
            "decision_date": grant.decision_date,
            "is_major": grant.is_major,
            "case_layer": None,
            "case_acres_gis": None,
            "plant_id": None,
            "match_type": "no_case_geometry",
            "distance_m": None,
        }
        if grant.cse_nr in case_geom.index:
            geom = case_geom.loc[grant.cse_nr, "geometry"]
            row["case_layer"] = case_geom.loc[grant.cse_nr, "layer"]
            row["case_acres_gis"] = round(shapely.area(geom) / 4046.856, 1)
            hits = tree.query(geom, predicate="dwithin", distance=CASE_MATCH_M)
            if len(hits) == 0:
                row["match_type"] = "none_within_1km"
            else:
                # Best hit: the plant overlapping the case most, else the nearest.
                case_words = name_words(grant.case_name, grant.customer)
                scored = []
                for i in hits:
                    pg = plants.geometry.values[i]
                    overlap = shapely.area(shapely.intersection(pg, geom)) if pg.geom_type != "Point" else 0.0
                    plant_words = name_words(*(plants[c].values[i] for c in ("plant_name", "entity_name", "uspvdb_name")))
                    scored.append((not (case_words & plant_words), -overlap, shapely.distance(pg, geom), i))
                no_name, _, distance, best = min(scored)
                row["plant_id"] = int(plants["plant_id"].values[best])
                row["distance_m"] = round(float(distance), 1)
                row["match_type"] = "intersects" if distance < 1.0 else "within_1km"
                row["name_match"] = not no_name
                row["n_candidate_plants"] = len(hits)
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Build


def build(data_dir: Path) -> None:
    started = time.monotonic()

    def print(*args):  # noqa: A001 - timestamped progress
        builtins.print(f"[{time.monotonic() - started:6.0f}s]", *args, flush=True)

    manifest = json.loads((data_dir / "sources.json").read_text())
    eia_path = Path(manifest["eia860m"]["file"])
    uspvdb_path = next((data_dir / "uspvdb" / "extracted").glob("*.geojson"))
    gdb_path = next((data_dir / "sma" / "extracted").glob("*.gdb"))
    cases_path = Path(manifest["blm_solar_cases"]["file"])

    print("Reading EIA-860M ...")
    capacity, plants = load_eia(eia_path)
    print(f"  {len(plants)} plants, {len(capacity)} plant-year rows")

    print("Reading USPVDB ...")
    uspvdb = load_uspvdb(uspvdb_path)
    matched = uspvdb[uspvdb["eia_id"].isin(plants.index)].set_index("eia_id")
    plants["uspvdb_case_id"] = matched["case_id"].reindex(plants.index).astype("Int64")
    plants["uspvdb_name"] = matched["p_name"].reindex(plants.index)
    plants["uspvdb_year"] = matched["p_year"].reindex(plants.index).astype("Int64")
    plants["uspvdb_cap_ac"] = matched["p_cap_ac"].reindex(plants.index)
    plants["geom_source"] = np.where(plants["uspvdb_case_id"].notna(), "footprint", "point")

    points = gpd.GeoDataFrame(
        plants[["plant_id"]], geometry=gpd.points_from_xy(plants["longitude"], plants["latitude"]), crs=4326
    ).to_crs(AREA_CRS)
    footprints = gpd.GeoDataFrame(
        {"plant_id": matched.index.values}, geometry=shapely.make_valid(matched.geometry.values), crs=AREA_CRS
    )
    print(f"  {len(footprints)} plants have a footprint")

    print("Reading SMA (about a minute) ...")
    sma = load_sma(gdb_path)
    print(f"  {len(sma)} polygons")

    print("Assigning owners ...")
    shares = footprint_shares(footprints, sma)
    print("  footprints done")
    # The point owner is computed for every plant: it is the assignment for
    # point plants and a consistency check for footprint plants.
    pts = point_owner(points, sma).set_index("plant_id")
    plants = plants.join(pts.add_prefix("point_"))
    print("  points done")

    by_cat = shares.groupby(["plant_id", "owner_category"])["area_m2"].sum().unstack(fill_value=0.0)
    for cat in CATEGORIES:
        if cat not in by_cat:
            by_cat[cat] = 0.0
    fp_area = footprints.set_index("plant_id").geometry.area
    fp_major = by_cat.idxmax(axis=1)
    top_agency = (
        shares[shares.owner_category != "unmapped"]
        .sort_values("area_m2", ascending=False)
        .drop_duplicates(["plant_id", "owner_category"])
        .set_index(["plant_id", "owner_category"])["agency_code"]
    )
    plants["footprint_area_km2"] = (fp_area / 1e6).reindex(plants.index)
    is_fp = plants["geom_source"] == "footprint"
    plants["owner_category"] = np.where(is_fp, fp_major.reindex(plants.index), plants["point_owner_category"])
    plants["owner_agency"] = [
        top_agency.get((pid, cat)) if fp else plants.at[pid, "point_owner_agency"]
        for pid, cat, fp in zip(plants.index, plants["owner_category"], is_fp)
    ]
    fed_share = (by_cat["blm"] + by_cat["other_federal"]) / fp_area
    blm_share = by_cat["blm"] / fp_area
    point_fed = plants["point_owner_category"].isin(FEDERAL).astype(float)
    point_blm = (plants["point_owner_category"] == "blm").astype(float)
    plants["federal_share"] = np.where(is_fp, fed_share.reindex(plants.index).clip(upper=1.0), point_fed)
    plants["blm_share"] = np.where(is_fp, blm_share.reindex(plants.index).clip(upper=1.0), point_blm)
    plants["mapped_share_sum"] = (by_cat.drop(columns="unmapped").sum(axis=1) / fp_area).reindex(plants.index)

    geom = pd.Series(shapely.to_wkt(points.to_crs(4326).geometry.values, rounding_precision=6), index=plants.index)
    fp_wkt = pd.Series(
        shapely.to_wkt(footprints.to_crs(4326).geometry.values, rounding_precision=6), index=footprints["plant_id"].values
    )
    geom.update(fp_wkt)
    plants["geom_wkt"] = geom

    print("Cross-checking BLM grants ...")
    used = gpd.GeoDataFrame(
        plants[["plant_id", "plant_name", "entity_name", "uspvdb_name"]].reset_index(drop=True),
        geometry=[
            footprints.set_index("plant_id").geometry.get(pid) if fp else pt
            for pid, fp, pt in zip(plants.index, is_fp, points.geometry.values)
        ],
        crs=AREA_CRS,
    )
    check = case_check(blm_grants(), cases_path, used)
    check = check.merge(
        plants[["plant_id", "plant_name", "owner_category", "blm_share", "geom_source", "first_year", "operating_mw", "planned_mw"]].reset_index(drop=True).rename(
            columns={"owner_category": "plant_owner_category", "blm_share": "plant_blm_share", "geom_source": "plant_geom_source"}
        ),
        on="plant_id",
        how="left",
    )

    # Reverse check: plants with no BLM solar case polygon (any disposition) nearby.
    cases = gpd.read_file(cases_path)
    cases = cases[cases.geometry.notna()].to_crs(AREA_CRS)
    case_tree = shapely.STRtree(cases.geometry.values)
    plants["blm_solar_case_within_1km"] = [
        len(case_tree.query(g, predicate="dwithin", distance=CASE_MATCH_M)) > 0 for g in used.geometry.values
    ]

    source_rows = []
    for key, label, version in (
        ("eia860m", "EIA-860M", re.sub(r"_generator(\d{4})\.xlsx$", r" \1", Path(manifest["eia860m"]["file"]).name).title()),
        ("uspvdb", "USPVDB", uspvdb_path.stem),
        ("sma", "BLM National SMA Surface Management Agency Area Polygons", f"ArcGIS item modified {manifest['sma'].get('item_modified', '')[:10]}"),
        ("blm_solar_cases", "BLM ROW case polygons (solar, all dispositions)", "live service"),
    ):
        entry = manifest[key]
        source_rows.append(
            {
                "source": label,
                "version": version,
                "url": entry["url"],
                "file": entry["file"],
                "sha256": sha256(Path(entry["file"])),
                "last_modified": entry.get("last_modified"),
                "downloaded_at": entry["downloaded_at"],
            }
        )

    plant_cols = [
        "plant_id", "plant_name", "entity_name", "state", "county", "latitude", "longitude",
        "first_year", "operating_mw", "planned_mw", "pre2016_mw",
        "geom_source", "uspvdb_case_id", "uspvdb_name", "uspvdb_year", "uspvdb_cap_ac", "footprint_area_km2",
        "owner_category", "owner_agency", "federal_share", "blm_share", "mapped_share_sum",
        "point_owner_category", "point_owner_agency", "point_owner_unit", "point_overlapping_categories",
        "point_boundary_distance_m", "point_near_boundary_500m", "blm_solar_case_within_1km", "geom_wkt",
    ]  # fmt: skip
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"drop table if exists {table}"))
        pd.DataFrame(source_rows).to_sql("place_source", conn, index=False)
        plants[plant_cols].to_sql("place_solar_plant", conn, index=False)
        capacity.to_sql("place_solar_capacity", conn, index=False)
        shares.to_sql("place_solar_land_share", conn, index=False)
        check.to_sql("place_blm_case_check", conn, index=False)
        conn.execute(text("alter table place_solar_plant add primary key (plant_id)"))
        conn.execute(text("create index on place_solar_capacity (plant_id)"))
        conn.execute(text("create index on place_solar_land_share (plant_id)"))
    print("Wrote", ", ".join(TABLES))


# --------------------------------------------------------------------------
# Report


def _pct(num: float, den: float) -> str:
    return "–" if not den else f"{100 * num / den:.0f}%"


def _mw(x: float) -> str:
    return f"{x:,.0f}"


def report() -> str:
    """Markdown tables for the write-up, from the place_* tables."""
    with engine.connect() as conn:
        plants = pd.read_sql("select * from place_solar_plant", conn).set_index("plant_id", drop=False)
        cap = pd.read_sql("select * from place_solar_capacity", conn)
        check = pd.read_sql("select * from place_blm_case_check", conn)
        sources = pd.read_sql("select * from place_source", conn)
    cap = cap.join(plants[["state", "owner_category", "federal_share", "geom_source"]], on="plant_id")
    # SMA codes almost all non-federal, non-state, non-tribal land in
    # California as UND, so private and undetermined are reported together.
    cap["group"] = cap["owner_category"].replace({"undetermined": "private", "local": "other", "unmapped": "other"})
    out: list[str] = []
    w = out.append
    groups = ("blm", "other_federal", "state", "tribal", "private", "other")

    def combined_table(df: pd.DataFrame) -> None:
        piv = df.pivot_table(index="year", columns="group", values="mw", aggfunc="sum", fill_value=0.0)
        piv = piv.reindex(columns=groups, fill_value=0.0)
        area = df.assign(f=df.mw * df.federal_share).groupby("year")["f"].sum()
        w("| Year | New MW | **Federal** | BLM | Other federal | State | Tribal | Private / undetermined | Other | Federal, area-weighted |")
        w("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
        for year in list(piv.index) + ["All"]:
            r = piv.sum() if year == "All" else piv.loc[year]
            a = area.sum() if year == "All" else area.loc[year]
            tot = r.sum()
            cells = [_pct(r[g], tot) for g in groups]
            label = "All years" if year == "All" else str(year)
            w(f"| {label} | {_mw(tot)} | **{_pct(r['blm'] + r['other_federal'], tot)}** | " + " | ".join(cells) + f" | {_pct(a, tot)} |")

    def state_table(df: pd.DataFrame) -> None:
        w("| Year | " + " | ".join(STATES) + " |")
        w("|---|" + "--:|" * len(STATES))
        for year in sorted(df.year.unique()) + ["All"]:
            sub = df if year == "All" else df[df.year == year]
            cells = []
            for st in STATES:
                x = sub[sub.state == st]
                tot = x.mw.sum()
                fed = x[x.group.isin(FEDERAL)].mw.sum()
                cells.append("–" if not tot else f"{_pct(fed, tot)} of {_mw(tot)}")
            w(f"| {'All years' if year == 'All' else year} | " + " | ".join(cells) + " |")

    op, pl = cap[cap.status == "operating"], cap[cap.status == "planned"]
    w("### Operating, by year online: five states")
    w("")
    combined_table(op)
    w("")
    w("### Operating, by year online: federal share of new MW, by state")
    w("")
    state_table(op)
    w("")
    w("### Planned, by expected year online: five states")
    w("")
    combined_table(pl)
    w("")
    w("### Planned, by expected year online: federal share of new MW, by state")
    w("")
    state_table(pl)
    w("")

    w("### Footprint vs point")
    w("")
    w("| | Plants | Footprint | Point | Share of MW on a footprint |")
    w("|---|--:|--:|--:|--:|")
    for label, df in (("Operating, 2016 on", op), ("Planned", pl), ("All", cap)):
        sub = plants.loc[sorted(set(df.plant_id))]
        nfp = (sub.geom_source == "footprint").sum()
        w(f"| {label} | {len(sub)} | {nfp} | {len(sub) - nfp} | {_pct(df[df.geom_source == 'footprint'].mw.sum(), df.mw.sum())} |")
    w("")
    fp = plants[plants.geom_source == "footprint"]
    agree = (fp.owner_category.isin(FEDERAL) == fp.point_owner_category.isin(FEDERAL)).sum()
    w(f"On footprint plants, the EIA point and the footprint agree on federal vs not for {agree} of {len(fp)} ({_pct(agree, len(fp))}).")
    w("")
    pt = plants[plants.geom_source == "point"]
    near = pt[pt.point_near_boundary_500m.astype(bool)]
    w("### Points within 500 m of an ownership boundary")
    w("")
    w("| Point plants | Within 500 m | Of which federal-assigned | Their MW | Within 100 m |")
    w("|--:|--:|--:|--:|--:|")
    w(
        f"| {len(pt)} | {len(near)} ({_pct(len(near), len(pt))}) | {near.owner_category.isin(FEDERAL).sum()} | "
        f"{_mw(cap[cap.plant_id.isin(near.index)].mw.sum())} | {(pt.point_boundary_distance_m < 100).sum()} |"
    )
    w("")
    w("Federal-assigned point plants near a boundary:")
    w("")
    w("| Plant | State | Owner | Distance (m) | MW | Year |")
    w("|---|---|---|--:|--:|--:|")
    for r in near[near.owner_category.isin(FEDERAL)].sort_values("point_boundary_distance_m").itertuples():
        w(f"| {r.plant_name} ({r.plant_id}) | {r.state} | {r.owner_agency} | {r.point_boundary_distance_m:.0f} | {_mw(r.operating_mw + r.planned_mw)} | {r.first_year} |")
    w("")

    w("### Tracker cross-check: BLM solar ROW grants 2016-2023")
    w("")
    n = len(check)
    hit = check[check.match_type != "none_within_1km"]
    on_blm = hit[hit.plant_owner_category == "blm"]
    w(
        f"{n} grants. {len(hit)} ({_pct(len(hit), n)}) have an EIA plant within {CASE_MATCH_M} m of the case polygon; "
        f"{len(on_blm)} ({_pct(len(on_blm), n)}) of those plants are assigned to BLM land "
        f"({hit.name_match.astype(bool).sum()} of the matches also share a distinctive word with the case name or customer)."
    )
    w("")
    w("| Case | Name | Decided | Match | Plant | Plant owner | Plant online |")
    w("|---|---|---|---|---|---|---|")
    for r in check.sort_values(["match_type", "decision_date"]).itertuples():
        if r.match_type == "none_within_1km":
            plant, owner, yr = "–", "–", "–"
        else:
            plant = f"{r.plant_name} ({int(r.plant_id)})"
            owner = r.plant_owner_category
            yr = f"{int(r.first_year)}" + (" planned" if r.planned_mw and not r.operating_mw else "")
        match = r.match_type.replace("_", " ") + ("" if r.match_type == "none_within_1km" or r.name_match else ", names differ")
        w(f"| {r.cse_nr} | {r.case_name} | {r.decision_date} | {match} | {plant} | {owner} | {yr} |")
    w("")

    w("### Plants assigned to other federal land")
    w("")
    w("| Plant | State | Agency | Source | MW |")
    w("|---|---|---|---|--:|")
    of = plants[plants.owner_category == "other_federal"].assign(mw=lambda d: d.operating_mw + d.planned_mw).sort_values("mw", ascending=False)
    for r in of.head(12).itertuples():
        w(f"| {r.plant_name} ({r.plant_id}) | {r.state} | {r.owner_agency} | {r.geom_source} | {_mw(r.mw)} |")
    w("")
    w("### BLM-assigned plants with no BLM solar case (any status) within 1 km")
    w("")
    nc = plants[(plants.owner_category == "blm") & ~plants.blm_solar_case_within_1km.astype(bool)]
    for r in nc.itertuples():
        w(f"- {r.plant_name} ({r.plant_id}), {r.state}, {r.geom_source}, {_mw(r.operating_mw + r.planned_mw)} MW, {r.first_year}")
    w("")
    w("### Unmapped plants (no SMA polygon at the point)")
    w("")
    for r in plants[plants.owner_category == "unmapped"].itertuples():
        w(f"- {r.plant_name} ({r.plant_id}), {r.state} {r.county}, {r.latitude}, {r.longitude}, {_mw(r.operating_mw + r.planned_mw)} MW")
    w("")
    w("### Sources")
    w("")
    w("| Source | Version | URL | Downloaded |")
    w("|---|---|---|---|")
    for r in sources.itertuples():
        url = r.url if len(r.url) < 120 else r.url.split("?")[0] + " (query)"
        w(f"| {r.source} | {r.version} | {url} | {r.downloaded_at} |")
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
