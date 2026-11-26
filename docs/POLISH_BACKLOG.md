# Portfolio polish backlog

Goal: make Almanac Grid read well to a hiring manager at a climate or energy
company in five minutes. Work the list top down, one item per pass. Each pass
goes on its own branch as a draft pull request; nothing is merged, pushed to
`main` or deployed without Samuel's review. Product claims stay to what's in
the repo or what Samuel has said: no invented metrics or users.

Status key: `[ ]` open, `[x]` done (with the PR that did it), `[?]` needs
Samuel's answer first.

## What a hiring manager sees today (review of Nov 26, 2026)

- **The live page** (almanac.samuellittlefield.com) is the main surface. It is
  careful, readable on a phone, and leads with a clear finding. Before pass 1
  it never said what Samuel built, why, or for whom: the only sign of him was
  the byline.
- **The repository is private**, so the README and code are invisible unless
  Samuel shares access. The GitHub description ("National weather,
  geological survey, and permitting-bill trend tracker") doesn't match the
  project.
- **The README** opened with setup and ingestion detail; there was no
  case-study framing.
- **Small gaps:** no favicon (`/favicon.ico` returns 404), no Open Graph tags
  (a LinkedIn share shows a bare link), "BAAJA" never spelled out.

## Backlog, in priority order

### Pass 1 (PR: portfolio-polish-1)

- [x] Live page: add an "About this project" section (problem, what it is,
  who it's for, what was left out and why, how it's built, Claude credited),
  linked from the header.
- [x] README: open with a case-study section (problem, what was built,
  audience, decisions, what it shows, how it was built).
- [x] Live page: Open Graph and Twitter card tags, an inline SVG favicon, and
  BAAJA spelled out on first use.

### Next

1. [?] **Decide how hiring managers reach the code.** Options: make the repo
   public, or keep it private and link only the live page. If public, first
   check the history for anything that shouldn't be (no `.env` is committed;
   `.gitignore` covers it) and fix the GitHub description and website field.
2. [ ] **Rebuild and deploy the page** with pass 1's template changes (needs
   the `snapshots/2026-11-19` snapshot, which is only on Samuel's machine; see
   "Public page" in the README). Check the About section on a phone.
3. [ ] **Social preview image.** A 1200x630 image of the lead chart for
   `og:image`, so a LinkedIn share shows the finding, not just the title.
4. [?] **Name the audience in one line at the top of the page.** The plan
   names developers, regulatory counsel and permitting consultants; Samuel
   has said litigators and developers look likeliest. Pick one primary reader
   and say who the page is for above the fold.
5. [ ] **A "what I'd build next" note** (page and README), drawn from
   `docs/plan.md` and `docs/spec-v2.md`: linking a pipeline's BLM
   right-of-way to its FERC certificate (v2.1), §1402 pattern detection on
   FERC and BLM together (v1.5). Shows product judgment beyond what shipped.
6. [ ] **Bring `docs/plan.md` up to date.** It still says S.5653 was
   introduced and not enacted; the page says BAAJA passed the Senate on
   November 19, 2026, as an amendment to H.R. 2347. Verify against the passed
   text (the page also flags the 10-year lookback as unverified) and update
   both.
7. [ ] **Surface the Place analyses.** Three write-ups in `docs/place-*.md`
   (federal land share of new solar, BLM filings vs. interconnection queues,
   filing outcomes) aren't linked from the page. Add a short "Other analyses"
   list, published as pages or linked from the README.
8. [ ] **Date labels.** The kicker says data as of November 17; Methodology
   says the snapshot was taken November 19. Both are right (fetch vs. dump),
   but one sentence explaining it would stop a careful reader tripping on it.
9. [ ] **Methodology length.** It's thorough, which suits counsel, but long
   for a hiring manager. Consider folding Definitions and Exclusions into
   `<details>` blocks so the page ends sooner.
10. [ ] **Page tests.** `site/build.py` has no tests. A small fixture snapshot
    and a test that the page renders would keep later polish passes safe.
