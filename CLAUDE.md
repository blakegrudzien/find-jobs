# find_jobs.py — working context

This is infrastructure supporting a live, active job search. Correctness and
not-breaking-what-works rank above adding features.

## Who this is for

Blake, a May 2026 Emory CS grad job-searching in the Bay Area for SWE / data /
backend and customer-facing technical (SE / FDE / FAE) roles.

## How the output is actually used

The CSV is read **top-down until about 5 consecutive roles are of no
interest** — historically because they turned out to require too much
experience. That single fact drives every design decision here: **precision at
the top of the list is the only thing that matters.** A false positive in the
top 50 costs real attention; a false negative at rank 300 costs nothing,
because rank 300 is never read.

Downstream of this script (Notion entry, contact research, outreach) is
deliberately manual. Don't automate it without asking.

## Workflow rules

- **Never `git commit` or `git push`.** Prepare changes in the working tree and
  leave them for Blake to review and commit himself.
- Public repo: https://github.com/blakegrudzien/find-jobs
- `jobs/*.csv` and `seen_urls.json` are gitignored on purpose — the CSVs
  contain named companies scored with `fit_score` and `crunch_flags`, which is
  not something to publish.

## Scoring — what each weight means and why

These were set deliberately in a calibration interview. Don't "clean them up."

| Component | Weight | Rationale |
|---|---|---|
| Role type | max 25 | backend + data = 25; SWE/platform/infra = 18; FDE/solutions = 12; support = 8 |
| New-grad in title | +20 | **Seniority signals are meant to outweigh role type.** Confirmed choice: the ranking answers "can I actually get this?" before "is this the ideal flavor?" |
| New-grad in body | max +18 | same |
| Years: stated floor of 0 or 1 | +15 | an explicit "1 year" — or an explicit "0-2 years" — means they set the level low on purpose. The best single signal. A range starting at 0 says they will take someone with no professional experience, so it earns the same as a stated 1 |
| Years: unstated | +10 | usually fine, but more ambiguous than an explicit low floor |
| Years: 2 stated | +6 | |
| Bay Area | +15 | |
| Remote | +4 | |
| Mentorship (`MENTOR_YOU`) | +8 | **deliberately light** — "mentorship"/"coaching" appear in boilerplate benefits copy, so the phrase match is weak evidence |
| `MENTOR_OTHERS` | −12 | implies they want a senior hire |
| `STARTUP_SIGNALS` | +6 | series A/B, greenfield, 0-to-1 |
| `TOO_EARLY_SIGNALS` | −8 | "founding engineer", "employee #", "first engineer" — signals an eng team too small to learn from, which is the *opposite* of what Blake wants |
| Crunch language | −5 each | a demerit he wants to *see*, not one that buries the posting |
| Later cohort | −10 | still worth applying to; just shouldn't outrank a role open to him now |

Summary buckets: 60+ "strong fit", 45–59 "worth a look".

### Board size is intentionally NOT scored

`board_size` is a CSV column only. It was removed from scoring after being
measured against 402 live boards: the companies with ≤5 open roles are
netlify, dremio, motive, prisma, gremlin — established firms hiring
selectively, **not** small engineering teams. The old penalty was docking
points for a hiring freeze. The aggregate trend exists but is weak (8.6% of
postings on tiny boards use founding-engineer language vs 1.5% on the
largest), and `TOO_EARLY_SIGNALS` measures the same thing directly from the
posting text. Don't reintroduce it.

## Filters (all four must pass)

1. **Title** — matches `TITLE_KEYWORDS`, and no `TITLE_DISQUALIFIERS`.
   Note `" ii"` is deliberately *not* a disqualifier: level-II roles still pass,
   they just no longer get a new-grad bonus.
2. **Years** — `years_required(body)` returns `(floor, ceiling)`. The filter
   screens on the **ceiling**, so "1-3 years" is 3 and is dropped. The score
   uses the **floor**, because a range carries two different facts: the top is
   what they will demand, the bottom is how low they set the bar. Scoring the
   ceiling made "0-2 years" rank below "1 year", which is backwards.
3. **Grad year** — dropped only if the *earliest* cohort named is more than
   `GRAD_HORIZON_MONTHS` (12) out. A winter-grad req counts; two cohorts out
   doesn't.
4. **Location** — Bay Area, or remote-from-the-Bay. A remote posting naming
   only a non-US region (Poland, EMEA, UK…) is dropped with the rest.

## What NOT to change without asking

- The years cutoff (2) and the title keyword/disqualifier lists encode real
  decisions about what Blake will apply to.
- `CRUNCH_FLAGS` and the crunch penalty are his own culture-screening criteria.
- No authentication, no non-public endpoints, nothing beyond what the four
  platforms' public documented job-board APIs already expose.

## Bugs already found and fixed — don't reintroduce

- `TITLE_NEWGRAD` once contained the bare substrings `"i "` and `" i"` to catch
  a level-I title. They matched any word starting with i, so "Software
  Engineer, **I**nfrastructure" and "**AI** Backend Engineer" took the full
  +20 — 16% of all output. Now a case-sensitive `\bI\b(?!I)`.
- `YEARS_PATTERN` let **20.6%** of over-experienced roles through (24% of the
  top 50) because the gap between "years" and "experience" was capped too
  short, ranges were read from the bottom end, en dashes weren't matched, and
  many requirements never say "experience" at all ("3+ years building product
  features"). Fixed; top 100 is now 0% over-experienced.
- `--prune` rebuilt slug files from `load_slugs()`, which strips comments,
  silently deleting every prior `# slug  (404)` record. Now rewrites line by
  line.
- `strip_html` stripped tags before unescaping entities, so Greenhouse (which
  serves its body HTML-escaped) kept literal `<p>` in the text being matched.
- `scrape_one` swallowed non-404 failures, making a timeout indistinguishable
  from "no matching roles" — which silently corrupts the run-to-run diff. Now
  retried and reported, and the seen-store is **not** updated on a run with
  failures.
- `leverdemo-8` is Lever's own demo board, not a company. Permanently in
  `EXCLUDE_SLUGS`.

## Known limitations

- Bay Area detection is a hardcoded city list, not geocoding.
- Scoring is hand-tuned against stated preferences, never validated against
  outcomes. The score is a proxy, not a measured predictor.
- 30 slugs appear in more than one slug file. `--show-dupes` lists them;
  identical JDs are collapsed by `dedup_key` (company + title + location), so
  a company with several genuinely different open roles still gets one row
  each.
