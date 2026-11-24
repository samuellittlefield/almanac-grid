"""Download the raw inputs for the "Place" layer (analysis.place_solar).

Each source goes into its own folder under data/place/<download date>/, with
the HTTP response headers saved next to it and a sources.json recording the
URL, version and download time. data/ is not in git.

    python -m analysis.place_fetch [--date YYYY-MM-DD] [--skip-sma]

Sources:
- EIA-860M, the newest monthly generator file listed on
  https://www.eia.gov/electricity/data/eia860m/
- USPVDB GeoJSON, https://energy.usgs.gov/uspvdb/data/
- BLM National SMA Surface Management Agency Area Polygons (file geodatabase,
  ArcGIS Online item 6bf2e737c59d4111be92420ee5ab0b46, about 1.1 GB)
- BLM solar right-of-way case polygons (all four disposition layers of BLM's
  national ROW case service), for the tracker cross-check. The tracker's
  own tables store no geometry.
- LBNL "Queued Up" project-level interconnection queue data. The landing
  page, https://emp.lbl.gov/queues, answers scripted requests with a
  Cloudflare 403, so the file URL is set by hand from that page (QUEUED_UP_URL;
  update it for a new edition). The file itself downloads normally.
- Census cartographic boundary counties (1:500k), for county land shares.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
from pathlib import Path

import requests

from scripts.ingest_blm import ROW_SERVICE, _get_json

USER_AGENT = "almanac-grid-place/0.1"
EIA_PAGE = "https://www.eia.gov/electricity/data/eia860m/"
USPVDB_URL = "https://energy.usgs.gov/uspvdb/assets/data/uspvdbGeoJSON.zip"
SMA_ITEM = "6bf2e737c59d4111be92420ee5ab0b46"
SMA_URL = f"https://www.arcgis.com/sharing/rest/content/items/{SMA_ITEM}/data"
QUEUED_UP_URL = "https://emp.lbl.gov/sites/default/files/2026-05/LBNL_Ix_Queue_Data_File_thru2025.xlsx"
QUEUED_UP_EDITION = "Queued Up 2026 Edition (data through end of 2025; file updated May 2026)"
COUNTIES_URL = "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_county_500k.zip"
STATES = ("CA", "NV", "AZ", "NM", "UT")
SOLAR_PRODUCTS = ("Solar Development Grant", "Solar Development Lease")


def latest_eia860m_url(session: requests.Session) -> str:
    page = session.get(EIA_PAGE, timeout=60).text
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)  # EIA leaves future months commented out
    links = re.findall(r'href="(/electricity/data/eia860m/[^"]*_generator\d{4}\.xlsx)"', page)
    if not links:
        raise RuntimeError("no EIA-860M generator file found on " + EIA_PAGE)
    return "https://www.eia.gov" + links[0]


def download(session: requests.Session, url: str, folder: Path, name: str) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=600) as response:
        response.raise_for_status()
        (folder / "headers.txt").write_text("\n".join(f"{k}: {v}" for k, v in response.headers.items()))
        with open(folder / name, "wb") as out:
            for chunk in response.iter_content(1 << 20):
                out.write(chunk)
    return {
        "url": url,
        "file": str(folder / name),
        "last_modified": response.headers.get("Last-Modified"),
        "downloaded_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


CASE_LAYERS = {0: "Authorized", 1: "Pending", 2: "Interim", 3: "Closed"}


def fetch_blm_solar_cases(session: requests.Session, folder: Path) -> dict:
    """Every parcel polygon of a solar ROW case in the five states, in all four
    disposition layers (a grant that has since expired or been relinquished
    is in the Closed layer; open applications are Pending / Interim)."""
    folder.mkdir(parents=True, exist_ok=True)
    products = ", ".join(f"'{p}'" for p in SOLAR_PRODUCTS)
    states = " OR ".join(f"GEO_STATE LIKE '%{s}%'" for s in STATES)
    where = f"BLM_PROD IN ({products}) AND ({states})"
    features = []
    for layer, layer_name in CASE_LAYERS.items():
        for feature in _query_all(session, f"{ROW_SERVICE}/{layer}/query", where):
            feature["properties"]["layer"] = layer_name
            features.append(feature)
    path = folder / "blm_solar_row_cases.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return {
        "url": f"{ROW_SERVICE}/{{0,1,2,3}}/query?where={where}",
        "file": str(path),
        "features": len(features),
        "downloaded_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _query_all(session: requests.Session, url: str, where: str) -> list[dict]:
    features, offset = [], 0
    while True:
        body = _get_json(
            session,
            url,
            {
                "f": "geojson",
                "where": where,
                "outFields": "CSE_NR,LEG_CSE_NR,CSE_NAME,GEO_STATE,BLM_PROD,CSE_DISP,CSE_DISP_DT,CUST_NM_SEC,RCRD_ACRS",
                "returnGeometry": "true",
                "outSR": 4326,
                "orderByFields": "OBJECTID",
                "resultOffset": offset,
                "resultRecordCount": 500,
            },
        )
        if "error" in body:
            raise RuntimeError(f"ArcGIS query failed: {body['error']}")
        batch = body.get("features", [])
        features += batch
        if len(batch) < 500 and not body.get("exceededTransferLimit") and not body.get("properties", {}).get("exceededTransferLimit"):
            break
        offset += len(batch)
    return features


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--skip-sma", action="store_true", help="the SMA download is 1.1 GB")
    parser.add_argument("--only", choices=["eia", "uspvdb", "sma", "blm_cases", "queued_up", "counties"], nargs="*")
    args = parser.parse_args()
    root = Path("data/place") / args.date
    manifest_path = root / "sources.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    want = set(args.only or ["eia", "uspvdb", "sma", "blm_cases", "queued_up", "counties"]) - ({"sma"} if args.skip_sma else set())

    if "eia" in want:
        url = latest_eia860m_url(session)
        manifest["eia860m"] = download(session, url, root / "eia860m", url.rsplit("/", 1)[1])
    if "uspvdb" in want:
        manifest["uspvdb"] = download(session, USPVDB_URL, root / "uspvdb", "uspvdbGeoJSON.zip")
    if "sma" in want:
        item = session.get(f"https://www.arcgis.com/sharing/rest/content/items/{SMA_ITEM}?f=json", timeout=60).json()
        manifest["sma"] = download(session, SMA_URL, root / "sma", item.get("name") or "SMA.gdb.zip")
        manifest["sma"]["item_modified"] = datetime.datetime.fromtimestamp(item["modified"] / 1000, datetime.UTC).isoformat()
    if "blm_cases" in want:
        manifest["blm_solar_cases"] = fetch_blm_solar_cases(session, root / "blm_cases")
    if "queued_up" in want:
        manifest["queued_up"] = download(session, QUEUED_UP_URL, root / "queued_up", QUEUED_UP_URL.rsplit("/", 1)[1])
        manifest["queued_up"]["edition"] = QUEUED_UP_EDITION
    if "counties" in want:
        manifest["counties"] = download(session, COUNTIES_URL, root / "counties", COUNTIES_URL.rsplit("/", 1)[1])

    root.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
