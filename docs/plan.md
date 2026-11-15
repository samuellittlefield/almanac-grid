# Almanac Grid — v1 Plan & Positioning

This doc exists so a build agent (or anyone else) can pick up this project cold
without replaying the brainstorm that produced it. It captures the *why* and
the scope decisions, not implementation detail — see `spec.md` for that.

## What this is

A federal permitting-outcomes tracker: a normalized database of permitting
decisions (approved / denied / withdrawn, and how long each took) by project
type, built so trends are visible over time instead of scattered across
agency dockets.

## The bill that motivated this (context, not a dependency)

S.5653, the "Bipartisan American Affordability and Jobs Act," 119th Congress.
**Status: introduced in the Senate, not enacted.** Nothing here should assume
it becomes law — the tracker has value on its own regardless of the bill's
fate, and the bill is used only as the sharpest framing if/when it advances.

Two provisions in it are directly relevant:

- **§1402 ("pattern of disparate treatment")** defines an exhaustive taxonomy
  of ~45 energy project types and creates a cause of action: a developer can
  sue if federal permitting shows a 5-year statistical pattern of
  denial/delay against their project type. If they make a prima facie
  showing, the government must produce raw year-by-year approval/denial/delay
  stats by project type or face an adverse inference. This is the sharpest
  "why would anyone pay for this" hook, once/if the bill passes.
- **NEPA reform (§§1101–1110)** imposes hard agency deadlines and is
  explicitly framed around reducing *interagency* delay via a designated lead
  agency. This matters for the data model decision below.

## The product angle

Build the normalized permitting-outcomes-by-type dataset as the core asset.
Treat the §1402 legal framing as marketing, not the foundation — the
underlying data is useful with or without the bill.

## Who it's for

FERC-regulated project developers, their in-house regulatory counsel, and
permitting consultants/law firms — i.e., the people who have actually tried
to get these projects built and felt the delay/uncertainty firsthand. Narrow,
high-value audience, not a mass-market consumer site.

## What we validated before committing to this scope

Checked whether 5+ years of historical federal permitting data is actually
obtainable (not just "exists somewhere behind a FOIA request"):

- **FERC eLibrary** — no official bulk API, but scrapable via docket search
  (date-range + docket-type queries), decades of depth. ✅ usable now.
- **Army Corps §404 (Clean Water Act / wetlands)** — mostly FOIA-gated; the
  public viewer exposes only ~10% of permit records and ~4% of jurisdictional
  determinations per independent analysis. ❌ not usable without a FOIA
  process.
- **BLM ePlanning / NEPA register** — scrapable, no confirmed bulk export.
- **EPA EIS database** — accessible for the EIS tier back to 1987, but EIS is
  the rare large-project tier; most actual decisions (EAs, categorical
  exclusions) aren't systematically public anywhere, federal or private.
- **Existing public datasets** — PNNL/DOE's NEPATEC/PermitAI corpus (~120–150k
  NEPA documents), the US Forest Service PALS dataset (CC0, fully
  downloadable, 2005–2021, pre-structured), and RFF's clean-energy NEPA
  timeline dataset (2009–2023) already exist publicly. This means the moat
  isn't "can we get any data" — it's normalizing it to a consistent taxonomy
  and building the trend/pattern layer, which nobody has packaged yet.

## Scope decisions

### In scope for v1

- FERC-regulated projects only: natural gas pipelines (NGA §7 certificates),
  LNG terminals, hydroelectric licensing, electric transmission
  (interconnection and the bill's new backstop siting authority).
- A trend view and project-level detail, backed by real ingested FERC data.

### Deferred, and why

- **BLM PALS integration** — public-domain and ready to use, but deferred to
  v2. The data model (below) is built so this attaches later without a
  rewrite.
- **Army Corps / CWA wetlands data** — FOIA-gated; revisit once v1 proves
  there's real demand worth the FOIA turnaround.
- **Transmission-siting opportunity layer** (wind/solar resource data
  overlaid with DOE's new §224 geographic-area designations) — a different,
  more crowded product (incumbents already do GIS-based siting/leasing
  tools), heavier to build, and depends on a DOE designation process that
  doesn't exist until the bill passes.
- **§1402 statistical "pattern" detection** — needs a trustworthy baseline
  dataset first; planned as v1.5 once the core tracker has real data and real
  users.
- **Geospatial/parcel-level tooling generally** — avoided for both
  market-crowding reasons (see above) and build-complexity reasons (heavy GIS
  libraries like GDAL/rasterio are the kind of C-extension-heavy packages
  that have already caused real friction in this project's toolchain).

## Why the data model isn't FERC-shaped

A pipeline or transmission line crossing federal land needs *both* a FERC
certificate and a BLM right-of-way grant for the same physical project — so
some future BLM records should attach to projects already in the system, not
create a parallel, disconnected dataset. The schema is a `project` entity
with a one-to-many `agency_action` table underneath it (see `spec.md`), so v2
can add BLM rows — some matched to existing FERC-sourced projects, some
standalone (e.g. solar/wind leasing, which has no FERC role at all) — without
a rewrite.

This also sets up something more interesting than a v2 nice-to-have: a
project's *total* cross-agency time-to-permit is the one number that could
actually show whether the bill's NEPA lead-agency reform worked. That's
unreachable with a single-agency-shaped schema.

## Non-goals for v1

No auth/accounts, no payments, no multi-agency data, no statistical
pattern-detection, no geospatial siting tools, no production deployment
beyond what's already set up (local dev + the mini worker).
