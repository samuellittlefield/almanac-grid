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

  **Note from implementation:** FERC's EL docket type — the only one that
  fit "electric" at ingestion time — turned out to be general electric
  complaint/rate-dispute dockets, not transmission siting. v1 ingests that
  data under `electric_complaint` instead; see docs/spec.md and the README's
  docket-type table. Real interconnection/siting data needs a different
  source and is not in v1.

### Deferred, and why

- **BLM data** — v2, now the next build; see "v2: BLM data" below and
  `spec-v2.md`. (Earlier drafts called this "BLM PALS"; PALS is the Forest
  Service's system, not BLM's.)
- **Army Corps / CWA wetlands data** — FOIA-gated; revisit once v1 proves
  there's real demand worth the FOIA turnaround.
- **Transmission-siting opportunity layer** (wind/solar resource data
  overlaid with DOE's new §224 geographic-area designations) — a different,
  more crowded product (incumbents already do GIS-based siting/leasing
  tools), heavier to build, and depends on a DOE designation process that
  doesn't exist until the bill passes.
- **§1402 statistical "pattern" detection** — still called v1.5, but now
  built *after* v2, so it runs on FERC and BLM data together. See "§1402 and
  what FERC data alone can show" below.
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

## §1402 and what FERC data alone can show

Checked against the bill text (Nov 2026). A "pattern of disparate treatment"
is a *substantial increase*, versus that project type's own previous 5
calendar years, in the share of permits improperly denied or delayed more than
60 days past the deadline (1 year from a complete application without NEPA
review; NEPA §107(g): 1 year for an EA, 2 for an EIS). On a prima facie
showing the government must produce, per year for 6 years, the number and
share of projects fully authorized, denied ≥1 permit, and pending >60 days
past deadline. That table is what v1.5 computes in advance.

Against FERC's 10 years of data:

- **Denials are ~0** (one gas pipeline denial, none for hydro or LNG), so the
  denial prong can never fire on FERC data; only the delay prong can.
- **Hydro is chronically past the bill's deadlines** (nearly every license,
  every year). A type that's always slow can't show an *increase* over its own
  baseline — a real finding about how §1402 would work in practice.
- **LNG has 2–8 decisions a year** — too few for a statistical test.
- **Gas pipelines (~40–80/yr)** are the one FERC type where the test means
  something.
- Review tier (EA vs EIS), which sets the deadline, *is* recoverable from
  eLibrary: 74 of 78 original CP applications in FY22–23 have an EA or EIS
  issuance on their docket.

So with FERC alone, v1.5 would amount to a gas-pipeline delay monitor. The
fights §1402 is aimed at — wind and solar on federal land — are BLM decisions,
which is why v2 comes first.

## v2: BLM data (decided Nov 2026)

Scope: BLM right-of-way decisions for **solar, onshore wind, electric
transmission, and oil/gas and CO2 pipelines**, plus **geothermal leases**,
for the same 10-year window. Full build detail in `spec-v2.md`.

- **Sources** (all public, no auth): BLM's national ROW case GIS layers for
  the case universe and current status; MLRS case actions for application
  dates and outcomes, decoded with BLM's LR2000 disposition codes (which,
  unlike FERC data, separate *rejected* from *withdrawn* — so the denial prong
  becomes measurable); ePlanning for NEPA review tier.
- **Biggest risk:** MLRS case actions come from an undocumented Salesforce
  endpoint that can break on Salesforce releases. Mitigated by caching and
  incremental refetch; the first backfill (~20k cases) is a long job suited
  to the mini.
- **Pipelines and transmission are noisy:** BLM data can't separate gas from
  oil pipelines or transmission from distribution lines, and most cases are
  small oilfield gathering lines. An `is_major` flag (has an EA or EIS)
  separates real projects for trend views.
- **Cross-agency matching** (the same pipeline's FERC certificate and BLM
  ROW) is v2.1: there's no shared ID, so it's fuzzy matching on applicant,
  state and dates, kept out of v2 to keep the build tractable.
- **Not in v2:** drilling permits (APDs — only aggregate stats are public),
  oil & gas leasing, mining, Forest Service, Army Corps.

## Non-goals for v1

No auth/accounts, no payments, no multi-agency data, no statistical
pattern-detection, no geospatial siting tools, no production deployment
beyond what's already set up (local dev + the mini worker).
