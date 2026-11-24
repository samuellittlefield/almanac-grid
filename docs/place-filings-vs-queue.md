# Place spike 2: BLM solar filings vs. solar interconnection requests

November 24, 2026. Analysis only; the public page is unchanged.

**Question.** Did developers file fewer solar applications with BLM relative
to overall solar demand in CA, NV, AZ, NM and UT? If interconnection requests
held up while BLM filings fell, developers moved away from federal land.

**Short answer: partly, measured against the 2020–22 peak. Not measured
against 2016–19.**

In counties that are at least 30% BLM land:
- **2016–19:** 19 BLM solar right-of-way filings per 100 solar
  interconnection requests.
- **2020–22:** 46 per 100, as BLM filings roughly tripled.
- **2023–25:** 32 per 100. Queue requests in those counties dipped only 13%
  (and their MW rose 18%), but BLM filings fell 39%.

Per GW requested, BLM filings halved from 2020–22 to 2023–25, which brought
them back to the 2016–19 rate. So the 2023–25 pattern matches the shift the
question describes, but only relative to a 2020–22 surge; it is not a fall
below the pre-2020 baseline. The same shape holds at 20% and 50% thresholds.
Two data gaps fall almost entirely in 2023–25, and correcting for them gives
a 2023–25 range of 28–41 per 100. That is still between the other two
periods. 2024 and 2025 are too thin on the queue side to read alone.

## Data

| Source | Version | Covers |
|---|---|---|
| Tracker BLM solar ROW cases (`project` / `agency_action`) | MLRS cache refreshed Nov 17, 2026 | Filings to mid-Nov 2026; 2026 is partial |
| LBNL Queued Up, project-level data file | **2026 Edition**; data through end of 2025, file updated May 2026 ([emp.lbl.gov/queues](https://emp.lbl.gov/queues)) | Requests through **2025** |
| BLM Surface Management Agency (SMA) layer | ArcGIS item modified Nov 9, 2026, already in `data/place/2026-11-24/` | |
| Census cartographic boundary counties, 1:500k | 2025 | 152 counties in the five states |
| BLM ROW case polygons (solar, all dispositions) | Live service, read Nov 24, 2026 | Used to put each case in a county, and for recorded acres |

The comparison runs **2016–2025**, the last year both sources cover. The ten
BLM filings dated 2026 are kept out of the ratios.

- **A BLM filing** is one MLRS serial in the tracker's `solar` type (Solar
  Development Grant or Lease) with a filing date. Its county is the one
  holding most of its case polygon.
- **A queue request** is any Queued Up row in the five states whose type
  includes Solar, solar+storage hybrids included, at any status, in the year
  it entered the queue. MW is the solar component only.
- **County BLM share** is the share of the county's area that SMA maps as
  BLM. The resulting state shares look right: NV 66%, UT 42%, AZ 17%, NM 17%,
  CA 15%.

## By year (BLM-heavy = at least 30% BLM land, 43 of 152 counties)

| Year | BLM-heavy: filings | requests | solar GW | filings per 100 req. | Other: filings | requests | solar GW | filings per 100 req. | All counties: filings per 100 req. |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 2016 | 16 | 72 | 13.2 | 22.2 | 0 | 125 | 20.5 | 0.0 | 8.1 |
| 2017 | 10 | 62 | 10.1 | 16.1 | 1 | 107 | 16.2 | 0.9 | 6.5 |
| 2018 | 16 | 72 | 15.1 | 22.2 | 2 | 112 | 21.2 | 1.8 | 9.7 |
| 2019 | 10 | 66 | 14.0 | 15.2 | 0 | 112 | 25.9 | 0.0 | 5.6 |
| 2020 | 31 | 41 | 10.2 | 75.6 | 10 | 132 | 30.7 | 7.6 | 23.6 |
| 2021 | 53 | 138 | 28.6 | 38.4 | 8 | 228 | 55.8 | 3.5 | 16.5 |
| 2022 | 48 | 111 | 24.5 | 43.2 | 13 | 103 | 27.2 | 12.6 | 28.8 |
| 2023 | 38 | 151 | 51.2 | 25.2 | 12 | 214 | 69.7 | 5.6 | 10.4 |
| 2024 | 26 | 51 | 11.4 | 51.0 | 6 | 37 | 11.3 | 16.2 | 52.3 |
| 2025 | 17 | 50 | 12.2 | 34.0 | 3 | 46 | 13.1 | 6.5 | 27.1 |
| 2026 (to mid-Nov) | 5 | – | – | – | 3 | – | – | – | – |

The "All counties" column also counts filings and requests that couldn't be
placed in a county (see Checks).

## By period, with threshold sensitivity

| Threshold | Period | BLM-heavy: filings | requests | solar GW | filings per 100 req. | filings per GW | Other: filings | requests | filings per 100 req. | All: filings per 100 req. |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| **30%** (43 counties) | 2016–19 | 52 | 272 | 52.3 | **19.1** | 0.99 | 3 | 456 | 0.7 | 7.5 |
| | 2020–22 | 132 | 290 | 63.4 | **45.5** | 2.08 | 31 | 463 | 6.7 | 21.6 |
| | 2023–25 | 81 | 252 | 74.8 | **32.1** | 1.08 | 21 | 297 | 7.1 | 18.1 |
| 20% (54 counties) | 2016–19 | 53 | 359 | 64.9 | 14.8 | | 2 | 369 | 0.5 | 7.5 |
| | 2020–22 | 153 | 365 | 86.9 | 41.9 | | 10 | 388 | 2.6 | 21.6 |
| | 2023–25 | 96 | 297 | 87.1 | 32.3 | | 6 | 252 | 2.4 | 18.1 |
| 50% (23 counties) | 2016–19 | 29 | 154 | 34.8 | 18.8 | | 26 | 574 | 4.5 | 7.5 |
| | 2020–22 | 80 | 132 | 41.0 | 60.6 | | 83 | 621 | 13.4 | 21.6 |
| | 2023–25 | 67 | 155 | 54.5 | 43.2 | | 35 | 394 | 8.9 | 18.1 |

All three thresholds give the same pattern: the ratio jumps in 2020–22 and
falls partway back in 2023–25.

- At 50%, the California desert counties drop into "other": San Bernardino
  is 45% BLM, Imperial 44%, Riverside 35%. Their filings move with them,
  which is why "other" filings jump at that threshold.
- Recorded acres, rather than counts, show the same thing. BLM-heavy counties
  had 2,400 BLM acres filed per GW of queue requests in 2016–19, 10,700 in
  2020–22 and 6,600 in 2023–25.

## Checks

**Queue requests with no county.** 144 of 2,174 requests from 2016–2025 have
no usable county, dropping 56.5 of 538.7 solar GW (10.5%) from the county
split. They still count in the "All" column.

| Entity | Requests | Solar MW | Entry years |
|---|--:|--:|---|
| CAISO | 137 | 54,244 | 2021, 2023 (135 of them in 2023) |
| APS | 3 | 1,000 | 2020–22 |
| WAPA-SN | 2 | 1,200 | 2017, 2018 |
| PNM, PacifiCorp | 2 | 8 | 2019, 2021 |

- Nearly all of the gap is CAISO's Cluster 15 (2023): 135 of its 254 solar
  requests, 54 of its 98 GW.
- One FIPS value was two county codes run together (603106019, which is Kings
  plus Fresno). The script keeps the first. Six other codes don't match a
  county in the request's state.
- Queued Up records only the first county of a multi-county request.

**BLM filings that couldn't be placed in a county.**
- 29 tracker cases in the five states have no filing date and aren't counted.
  The README's coverage section already notes these.
- 25 counted filings have no mapped polygon on BLM's case layer: mostly
  pending cases filed 2023–2025 that BLM hasn't mapped yet, 13 of them in
  California. 22 of the 25 are from 2023–25.

**Sensitivity of 2023–25 (30% threshold) to both gaps.**
- Unplaced BLM filings all assumed BLM-heavy: 103 filings, 40.9 per 100
  requests.
- CAISO's unplaced requests split the way its placed 2023 requests did (25%
  BLM-heavy by count and MW): 286 requests and 88 GW. That gives 28.3 per 100
  without the extra filings and 36.0 with them.
- The range is therefore **28–41 per 100, and 0.9–1.4 filings per GW**,
  against 45.5 and 2.08 in 2020–22 and 19.1 and 0.99 in 2016–19.

**Against LBNL's published figures (same edition).** The data file's
"11. Active Cap. Maps" sheet gives active solar capacity by state. Filtering
the project-level rows the same way (active status, type includes Solar,
solar MW) reproduces it exactly:

| State | Active solar GW, end-2025 (ours = LBNL) | Of which entered in 2025 (ours = LBNL) |
|---|--:|--:|
| CA | 64.12 | 0.32 |
| NV | 29.20 | 4.57 |
| AZ | 50.16 | 0.96 |
| NM | 14.98 | 6.73 |
| UT | 18.20 | 4.49 |
| **Five states** | **176.65** | **17.08** |

LBNL's "05. Annual Requests" sheet counts every resource type nationwide, so
it can't be compared with the five-state solar totals.

**Partial years.**
- **BLM 2026** runs to mid-November and is excluded from the ratios.
- **BLM 2024–2025** may still be filling in, if MLRS records applications
  late. The data can't show such a lag.
- **Queue 2025** is a full year in this edition. LBNL notes that some
  utilities' withdrawn and operational data may be delayed.

## Looks off

1. **The 2024–2025 queue years are hollow.**
   - Five-state solar requests fell from 500 in 2023 to 88 in 2024 and 96 in
     2025.
   - CAISO took solar requests every year from 2016 to 2021 (Cluster 14 was
     2021), then none in 2022, 254 in 2023 (Cluster 15), and none in 2024 or
     2025. Other utilities were moving to Order 2023 cluster processes at the
     same time.
   - That is why the single-year ratios for 2024 (52 per 100 overall) and
     2025 (27) are not demand signals.
   - Even the 2023–25 period may understate demand, since it holds just one
     CAISO window. Cluster 16, if it opens in 2026, would land in the next
     edition.
2. **BLM filings are serials, not projects.** Projects routinely hold several
   serials (the Place first pass found 38 grants covering 22 plants), and the
   2024 California filings include batches of small "community solar"
   applications (Gale, Harvard, Hinkley, Tortilla). Counts move with how
   developers split cases. Recorded acres show the same pattern, so it isn't
   only a count artifact. 18 counted cases have zero or no recorded acres.
3. **Neither measure is a pure read of demand.**
   - Queue requests are an inflated proxy: a project can enter several
     queues or re-enter a later cluster, and most are withdrawn. 2023
     withdrawals include CAISO Cluster 15 requests not resubmitted in 2024.
   - The queue's county is often the point of interconnection rather than
     the array site, so a request on a substation in a low-BLM county can be
     for a project on BLM land, and vice versa.
4. **BLM filings occur in "other" counties too.**
   - At the 30% threshold there are 55. Nearly half (26) are in **Maricopa
     County, AZ, which is 29% BLM**, just under the cut. Maricopa also holds a
     large share of Arizona's queue requests, so the 30% grouping is
     sensitive to that one county. The 20% threshold, which moves Maricopa
     into "BLM-heavy", gives nearly the same 2023–25 ratio (32.3 vs 32.1).
   - So the "other" column isn't a clean no-BLM control. It is better read as
     a check that BLM filings concentrate where BLM land is, which they do:
     265 of the 320 placed filings (83%) are in BLM-heavy counties at 30%.
5. **The 2020–22 baseline is itself unusual.** BLM solar filings in BLM-heavy
   counties went from 10–16 a year to 31–53. Whether 2023–25 is a retreat or
   a return to normal depends on which baseline you pick. Most of the
   difference is in BLM filings, not in the queue.

## Where it lives

- Code: `python -m analysis.place_fetch --only queued_up counties` downloads
  the two new inputs into `data/place/2026-11-24/` (not in git; recorded in
  `sources.json`). Note that the LBNL landing page blocks scripted requests,
  so the file URL is pinned in `analysis/place_fetch.py`. Then
  `python -m analysis.place_filings build` writes the tables, and
  `python -m analysis.place_filings report` prints the tables above.
- New tables, dropped and rebuilt on each build. No existing table is
  modified.
  - `place_filings_source`: inputs, with URL, version, SHA-256 and download
    time.
  - `place_county_land`: BLM and federal share per county.
  - `place_blm_solar_filing`: one row per tracker solar case in the five
    states, with filing year, county, county method and acres.
  - `place_queue_solar_request`: one row per solar queue request, with solar
    and total MW, county and county method.
  - `place_filings_vs_queue`: every year, period and county group at 20%,
    30% and 50%, with filings, acres, requests, MW and both ratios.
