# find_jobs

A job-board scraper for new-grad and early-career engineering roles. It reads a
list of company slugs, pulls every open posting from that company's Greenhouse,
Ashby, or Lever board, filters out anything asking for more experience than I
have, scores what's left, and writes a ranked CSV.

I built it because browsing ~40 VC portfolio company career pages by hand
surfaced 4 relevant roles. Pointed at ~440 companies, the same afternoon's work
surfaces a few hundred, ranked, with the over-experienced ones already removed.

## How it works

**Input** — three text files (`companies_greenhouse.txt`, `companies_ashby.txt`,
`companies_lever.txt`), one company slug per line. `#` comments and blanks are
ignored. Slugs come straight out of posting URLs:

```
job-boards.greenhouse.io/AIRBYTE/jobs/123  -> airbyte
jobs.ashbyhq.com/CLICKHOUSE/abc            -> clickhouse
jobs.lever.co/EXAMPLE/xyz                  -> example
```

I grow the lists with Google: `site:jobs.ashbyhq.com "new grad"`.

**Fetching** — each platform publishes a documented, unauthenticated job-board
API, which is what this reads. Nothing is scraped from rendered HTML, nothing is
behind a login, and one run is a few hundred GETs across 12 threads.

| Platform | Endpoint |
|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}` |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` |

**Filtering** — a posting has to clear three gates:

- **Title** contains a role keyword (backend, data engineer, forward deployed,
  solutions engineer, …) and no seniority marker (senior, staff, principal,
  manager, III, IV, …).
- **Experience** — the maximum years-of-experience the body asks for is at
  most 2.
- **Graduation year** — a posting naming a class year is dropped only if the
  *earliest* date it will accept is more than 12 months out, so a winter-grad
  req still counts but a two-cohorts-out req doesn't.

**Scoring** — an additive 0–100ish heuristic over role type, new-grad language
in the title and body, years required, Bay Area location, whether the posting
offers mentorship or expects you to provide it, company size (proxied by how
many roles the board has open), and a penalty for crunch-culture language
("run through walls", "whatever it takes", "80 hours").

**Output** — `jobs.csv`, ranked, plus `jobs_new.csv` containing only postings
not seen in a previous run. A `seen_urls.json` store tracks what's already been
reported, and it is deliberately *not* updated on a run where any board failed
to fetch — otherwise every posting from a board that timed out would resurface
as brand new the next week.

## Usage

```bash
pip install -r requirements.txt

python find_jobs.py                    # -> jobs.csv + jobs_new.csv
python find_jobs.py -o out.csv         # choose the output path
python find_jobs.py --workers 16       # more concurrency
python find_jobs.py --prune            # comment out slugs that 404'd
python find_jobs.py --show-dupes       # slugs listed under >1 platform
```

## Notes on precision

The filter's only real job is to keep unqualifiable roles off the top of the
list, since the list is read top-down until it stops being useful. Measured
against 5,568 live postings from 90 boards, an earlier version of the years
regex let **20.6%** of over-experienced roles through the filter — **24% of the
top 50** — for four separate reasons:

- the gap between "years" and "experience" was capped too short, so
  `5+ years of professional software engineering experience` never matched
- ranges were read from the bottom end, so `1-3 years` parsed as 1 and then
  collected the *largest* new-grad bonus in the score
- en dashes (`3–5 years`) weren't matched at all
- plenty of requirements never say "experience": `3+ years building product
  features`, `6+ years in infrastructure`

Fixing those took the top 100 to 0% over-experienced.

Similarly, the title-level new-grad bonus was keyed on the substrings `"i "`
and `" i"` to catch a level-I title. Those matched any word starting with i, so
"Software Engineer, **I**nfrastructure" and "**AI** Backend Engineer" collected
the full bonus — 16% of all output. It's a case-sensitive `\bI\b(?!I)` now.

## Known limitations

- Bay Area detection is a hardcoded city list, not geocoding.
- Board size is a weak proxy for company size — it conflates "small company"
  with "few roles open right now", so it's weighted lightly on purpose.
- The scoring weights are hand-tuned against my own preferences, not validated
  against outcomes. The score is a proxy, not a measured predictor.
