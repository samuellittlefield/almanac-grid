# Place spike 3: what happened to BLM solar filings, by filing period

November 24, 2026. Analysis only; the public page is unchanged. This builds
on [place-filings-vs-queue.md](place-filings-vs-queue.md).

**Scope.** BLM solar right-of-way filings in CA, NV, AZ, NM and UT from
2016–2025, from the tracker, split by county at the 30% BLM-land threshold.
The data is as of November 17, 2026, the last MLRS cache refresh. Each filing
is one MLRS serial, and the 29 cases with no filing date are left out.

**Short answer.**
- **Grants:** at equal age, the grant rate barely moved. In BLM-heavy
  counties, 10% of 2016–19 filings, 9% of 2020–22 filings and 11% of the
  qualifying 2023 filings were granted within 36 months.
- **Rejections and pending:** these are what changed. Rejections within 36
  months fell from 21% to 3%. The share still pending at 36 months rose from
  38% to 64%, then 69%. More than half of the 2020–22 filings are still
  pending today, 3–6 years on.
- **The 2020–22 surge was broad.** The number of distinct applicants grew
  about as fast as filings (40 → 105), and the top 5 applicants' share fell
  from 29% to 21%.

## Current status (as of November 17, 2026)

| Group | Period | Filings | Granted | Rejected | Withdrawn | Closed, no reason | Pending |
|---|---|--:|--:|--:|--:|--:|--:|
| **BLM-heavy** | 2016–19 | 52 | 19% | 29% | 33% | 12% | 8% |
| | 2020–22 | 132 | 14% | 3% | 15% | 11% | 56% |
| | 2023–25 | 81 | 7% | 2% | 11% | 2% | 77% |
| **Other** | 2016–19 | 3 | 0% | 33% | 33% | 0% | 33% |
| | 2020–22 | 31 | 19% | 3% | 13% | 10% | 55% |
| | 2023–25 | 21 | 0% | 10% | 19% | 5% | 67% |
| No county | 2020–22 | 1 | | | | | 100% |
| | 2023–25 | 22 | 0% | 0% | 5% | 5% | 91% |
| **All** | 2016–19 | 55 | 18% | 29% | 33% | 11% | 9% |
| | 2020–22 | 164 | 15% | 3% | 15% | 11% | 56% |
| | 2023–25 | 124 | 5% | 3% | 11% | 3% | 77% |

Current status flatters older periods simply because they've had longer to
be decided, so the next table compares periods at equal age.

## Status 36 months after filing

A filing qualifies if its 36-month mark falls on or before November 17, 2026,
which means it was filed by November 17, 2023. **For 2023–25, only filings
from January 1 to November 17, 2023 qualify: 36 of 81 in BLM-heavy counties,
12 of 21 in other counties, 2 of 22 with no county, and 50 of 124 overall.**
In effect, the 2023–25 row is the 2023 filings.

| Group | Period | Qualifying / all | Granted | Rejected | Withdrawn | Closed, no reason | Pending at 36 mo |
|---|---|--:|--:|--:|--:|--:|--:|
| **BLM-heavy** | 2016–19 | 52 / 52 | 10% | 21% | 29% | 2% | 38% |
| | 2020–22 | 132 / 132 | 9% | 3% | 13% | 11% | 64% |
| | 2023–25 | 36 / 81 | 11% | 3% | 17% | 0% | 69% |
| **Other** | 2016–19 | 3 / 3 | 0% | 33% | 33% | 0% | 33% |
| | 2020–22 | 31 / 31 | 13% | 3% | 13% | 10% | 61% |
| | 2023–25 | 12 / 21 | 0% | 17% | 25% | 0% | 58% |
| **All** | 2016–19 | 55 / 55 | 9% | 22% | 29% | 2% | 38% |
| | 2020–22 | 164 / 164 | 10% | 3% | 13% | 10% | 64% |
| | 2023–25 | 50 / 124 | 8% | 6% | 18% | 0% | 68% |

How to read it:
- **BLM-heavy counties.** About one filing in ten is granted within three
  years in every period. In 2016–19 most of the rest were resolved one way or
  another: rejected, withdrawn or closed. In 2020–22 and 2023 most of the rest
  are still open.
- **Other counties** follow the same pattern, but with 3 filings in 2016–19
  their percentages aren't meaningful.

## Applicants

The applicant is the case's customer name, normalized for case, punctuation,
"C/O" and "ATTN" tails and corporate suffixes. A *family* is cruder: the first
distinctive word of the name, plus a few aliases (IP Land Holdings with
Intersect Power, Naturgy Candela with Candela, NV Energy with Nevada Energy).
That way, project LLCs such as Blythe Solar II, III and IV count as one
family.

| Period | Filings | Distinct applicants | Filings per applicant | Top-5 applicants' share | Distinct families | Top-5 families' share |
|---|--:|--:|--:|--:|--:|--:|
| 2016–19 | 55 | 40 | 1.4 | 29% | 31 | 35% |
| 2020–22 | 164 | 105 | 1.6 | 21% | 91 | 27% |
| 2023–25 | 124 | 88 | 1.4 | 22% | 77 | 25% |

BLM-heavy counties alone show the same pattern. Top-5 applicants' share is
29% → 23% → 20%; top-5 families' share is 35% → 30% → 26%.

Top applicants (filings):
- **2016–19:** 174 Power Global (6), Harry Allen Solar (3), IP Land Holdings
  (3), First Solar Development (2), Celtic Energy (2)
- **2020–22:** EDF Renewable Development (13), Boulevard Associates (9), IP
  Land Holdings (7), Elisabeth Solar (3), RENRG Partners (3)
- **2023–25:** Naturgy Candela Devco (8), Innergex Renewable Development USA
  (8), Sawtooth Devco (4), MARS Energy Group (4), EDF Renewable Development
  (3)

**So the 2020–22 surge was not a few developers filing many serials.**
Filings tripled (55 → 164) while distinct applicants grew 2.6 times (40 →
105). Filings per applicant hardly changed (1.4 → 1.6), and concentration
fell. A few large developers did file a lot:
- EDF (14 filings in the family) and Intersect (10).
- NextEra probably sits behind both Boulevard Associates (9) and the Blythe
  Solar project LLCs (8), and EDF behind the Maverick Solar LLCs (4).
- If those are merged, NextEra and EDF are each about 10–11% of 2020–22
  filings, and the top-5 families' share rises to roughly 29–31%. That is
  still below 2016–19's 35%.

## Caveats and things that look off

1. **Serials, not projects.** Outcome shares are per MLRS serial, and big
   projects hold several. Four Blythe grants dated June 2023, for plants
   built in 2016–2020, count as four grants. The first Place report flags them
   as probably reissued grants rather than construction approvals.
2. **"Closed, no reason" is a real category, not a gap in this analysis.**
   BLM recorded a closure with no rejection, withdrawal or grant (see the
   README's coverage section). Within 36 months, 10–11% of 2020–22 filings
   ended that way, against 2% of 2016–19 filings. Some of these may be
   withdrawals or rejections in practice.
3. **The 2016–19 rejections stand out.** 21% of those filings were rejected
   within 36 months, against 3% since. The data doesn't say why. Possible
   reasons are a policy change, BLM rejecting incomplete or out-of-zone
   applications more readily then, or BLM now leaving such cases open instead
   of rejecting them. Telling these apart would mean reading the case files.
4. **One grant is dated before its filing** (CACA105845181, Arlington Solar:
   filed March 31, 2021, granted September 11, 2020). It counts as granted
   within 36 months. It is probably a filing-date error in MLRS.
5. **Pending is as reported by BLM's layer and MLRS.** A case BLM has
   effectively dropped but not closed stays pending, so long-pending shares
   may overstate live applications.
6. **Applicant families are approximate.**
   - First-word grouping merges some unrelated firms. The "Desert" family (4
     filings in 2020–22) is probably several developers.
   - It also misses parents that file under unrelated project names.
   - Some groupings are real, though. "Phoenix" is four 2023 LLCs named after
     Shakespeare characters (Juliet, Titania, Prospero, Portia), which looks
     like one developer's batch. Five filings list the applicant as
   "Unknown". The concentration figures are therefore a rough upper bound for
   applicants and a rough guide for families.
7. **Counties.** The 22 filings from 2023–25 with no mapped case polygon
   (mostly pending, 13 in California) are a separate "no county" group, so
   they don't count in BLM-heavy or other. The "All" rows include them.

## How to rerun

```bash
.venv/bin/python -m analysis.place_filing_outcomes
```

It reads `place_blm_solar_filing` and `place_county_land`, which
`analysis.place_filings build` creates, plus the MLRS cache date. It prints
the tables above and writes nothing.
