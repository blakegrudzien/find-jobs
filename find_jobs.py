#!/usr/bin/env python3
"""
Pulls open roles from Greenhouse, Ashby, and Lever job boards.

Reads company slugs from plain text files so you can grow the list
without editing this script.

Setup:
    pip install requests
    # companies_greenhouse.txt / companies_ashby.txt / companies_lever.txt
    # one slug per line; blank lines and # comments ignored

Usage:
    python find_jobs.py
    python find_jobs.py -o jobs.csv
    python find_jobs.py --workers 16     # faster
    python find_jobs.py --prune          # comment out dead slugs in the files

Finding slugs — look at any job posting URL:
    job-boards.greenhouse.io/AIRBYTE/jobs/123  -> airbyte
    jobs.ashbyhq.com/CLICKHOUSE/abc            -> clickhouse
    jobs.lever.co/EXAMPLE/xyz                  -> example

Bulk-collect with Google:
    site:job-boards.greenhouse.io "data engineer" "san francisco"
    site:jobs.ashbyhq.com "new grad"
    site:jobs.lever.co "backend engineer"
"""

import csv
import re
import json
import os
import sys
import html
import datetime
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

SLUG_FILES = {
    "greenhouse": "companies_greenhouse.txt",
    "ashby": "companies_ashby.txt",
    "lever": "companies_lever.txt",
}

# Boards that are not real companies. leverdemo-8 is Lever's own demo board:
# it has many generic-looking postings that score well and it is never a real
# lead. Skipped on every source regardless of which slug file it appears in.
EXCLUDE_SLUGS = {"leverdemo-8"}

TITLE_KEYWORDS = [
    "data engineer", "backend", "back-end", "back end",
    "platform engineer", "infrastructure engineer", "software engineer",
    "solutions engineer", "solution engineer", "sales engineer",
    "forward deployed", "customer engineer", "application engineer",
    "field application", "implementation engineer", "technical consultant",
    "solutions consultant", "solutions architect", "analytics engineer",
    "integration engineer", "developer support", "support engineer",
    "deployed engineer", "technical support engineer",
]

TITLE_DISQUALIFIERS = [
    "senior", "staff", "principal", "lead ", " lead", "director",
    "manager", "head of", "vp ", "vice president",
    " iii", " iv", "sr.", "sr ", "distinguished", "fellow",
    "intern", "internship",
]

# Matches "5+ years of professional software engineering experience",
# "3-5 years", "3–5+ yrs", "8 to 10 years", "3+ years building production
# software". Two things the older, tighter pattern got wrong and that let a
# third of the list through as false positives:
#   - the gap between "years" and the qualifying word allowed only [\s\w,],
#     so any "of professional software engineering experience" (or an
#     apostrophe, slash, or hyphen) broke the match
#   - a requirement is often not phrased with the word "experience" at all
#     ("3+ years building product features", "6+ years in infrastructure")
YEARS_PATTERN = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?"
    r"(?:years?|yrs?)\b[^.;•\n]{0,45}?"
    r"(?:experience|exp\b|background|building|working|develop|engineer"
    r"|industry|professional|in\s)",
    re.IGNORECASE,
)

# "4 year accredited university" is a degree requirement, not an experience
# requirement. Scrubbed from the body before scanning rather than skipped at
# match time, so a real requirement later in the same sentence ("...from a
# 4 year accredited university, 2+ years experience") still registers.
DEGREE_PATTERN = re.compile(
    r"\d{1,2}\s*[-–]?\s*year\s+(?:accredited|degree|university|college"
    r"|program|institution|bachelor)",
    re.IGNORECASE,
)
MAX_YEARS = 2

# --- graduation-year targeting ----------------------------------------------
# New-grad reqs are class-year specific. A "New Grad (December 2027)" posting
# is for a different cohort and is a wasted read, but a winter-2026 grad date
# is close enough that plenty of companies will take a May 2026 grad. So the
# rule is "drop it only if the EARLIEST date it will accept is more than
# GRAD_HORIZON_MONTHS out"; a posting that names no class year is kept.
GRAD_HORIZON_MONTHS = 12

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

# An optional month followed by a plausible year.
DATE_PATTERN = re.compile(
    r"\b(?:(" + "|".join(MONTHS) + r")\s+)?(20[2-9]\d)\b", re.IGNORECASE)

# Words that make a nearby year a graduation date rather than a copyright
# line or a "founded in 2019".
GRAD_CUE = re.compile(r"graduat|class of|new grad|commencement|degree by"
                      r"|diploma", re.IGNORECASE)


def grad_dates(text):
    """(year, month) pairs that look like a required graduation date.

    A bare year is treated as June — the usual spring commencement — so
    "class of 2027" reads as 2027-06 rather than 2027-01.
    """
    found = []
    for m in DATE_PATTERN.finditer(text):
        window = text[max(0, m.start() - 60):m.end() + 40]
        if not GRAD_CUE.search(window):
            continue
        month = MONTHS[m.group(1).lower()] if m.group(1) else 6
        found.append((int(m.group(2)), month))
    return found


def grad_year_too_far(text, today=None):
    """True if every graduation date named is beyond the horizon."""
    dates = grad_dates(text)
    if not dates:
        return False                     # no class year named -> keep it
    today = today or datetime.date.today()
    horizon_month = today.month + GRAD_HORIZON_MONTHS
    cutoff = (today.year + (horizon_month - 1) // 12,
              (horizon_month - 1) % 12 + 1)
    return min(dates) > cutoff

CRUNCH_FLAGS = [
    "60 hours", "70 hours", "80 hours", "hours per week", "hour weeks",
    "extreme ownership", "work hard play hard", "hero mode",
    "nights and weekends", "always on", "no work-life balance",
    "outsized hours", "long hours", "grind", "24/7 availability",
    "sacrifice", "run through walls", "lose sleep", "whatever it takes",
]

GOOD_FLAGS = [
    "new grad", "new graduate", "early career", "entry level",
    "entry-level", "recent graduate", "recent college", "mentorship",
    "mentor", "rotational", "0-2 years", "0-3 years", "1+ year",
    "1-3 years", "internships count", "less than two years",
    "training program", "no prior experience",
]

BAY_AREA = [
    "san francisco", "bay area", "palo alto", "menlo park", "mountain view",
    "san mateo", "san jose", "sunnyvale", "santa clara", "redwood city",
    "oakland", "berkeley", "fremont", "milpitas", "alameda", "burlingame",
    "cupertino", "foster city", "emeryville", "south san francisco",
    "san carlos", "belmont", "los altos", "campbell",
]

# --- scoring ---------------------------------------------------------------

# Explicit new-grad language in the body — strongest possible signal
EXPLICIT_NEWGRAD = [
    "new grad", "new graduate", "recent graduate", "recent college",
    "early career", "entry level", "entry-level", "less than two years",
    "no prior experience", "internships count", "rotational program",
    "graduating", "university graduate", "0-2 years", "0-1 years",
]

# Same, but in the title — even stronger.
# These used to include the bare tokens "i " and " i", meant to catch a level-I
# title ("Software Engineer I"). As plain substrings they matched any word
# starting with i, so "Software Engineer, Infrastructure", "AI Backend
# Engineer" and "Software Engineer II" all collected the full +20. That was
# 16% of the output. Level-I is now matched separately as a roman numeral.
TITLE_NEWGRAD = [
    "new grad", "new graduate", "early career", "associate",
    "university graduate", "entry level", "entry-level", "junior", "graduate",
]

# Standalone roman numeral I, case-sensitive so it can't match inside "AI".
# The negative lookahead keeps it off "II"/"III".
TITLE_LEVEL_ONE = re.compile(r"\bI\b(?!I)")

# Titles closest to what you actually want, highest first
TITLE_PRIORITY = [
    (["data engineer", "data platform", "data infrastructure",
      "analytics engineer"], 25),
    (["forward deployed", "deployed engineer", "customer engineer",
      "solutions engineer", "solution engineer"], 22),
    (["backend", "back-end", "back end", "platform engineer",
      "infrastructure engineer"], 20),
    (["software engineer"], 15),
    (["solutions architect", "solutions consultant", "sales engineer",
      "technical consultant", "application engineer"], 12),
    (["support engineer", "technical support"], 8),
]

# "you will mentor others" implies seniority — penalize
MENTOR_OTHERS = [
    "mentor other", "mentor junior", "mentor teammates", "mentor the team",
    "mentoring other", "mentor engineers", "raise the bar", "mentor peers",
]

# "you will be mentored" — what you actually want
MENTOR_YOU = [
    "mentorship", "you'll be mentored", "learn from experienced",
    "dedicated mentor", "guidance from senior", "with guidance",
    "supported by", "coaching",
]

# Startup signals in the JD body
STARTUP_SIGNALS = [
    "founding", "first engineer", "early employee", "small team",
    "employee #", "seed", "series a", "series b", "join us early",
    "one of our first", "ground floor", "early-stage", "early stage",
    "wear many hats", "0 to 1", "0-to-1", "greenfield",
]

# Board size thresholds — total roles posted, before filtering.
# The weights are deliberately small: board size conflates "small company"
# with "few roles open right now", so it was the largest single non-title
# lever in the score while being the least trustworthy input. Halved from
# +12/-8 so it can nudge ordering without deciding it.
SMALL_BOARD = 40      # likely a startup
LARGE_BOARD = 200     # likely a big company
SMALL_BOARD_BONUS = 6
LARGE_BOARD_PENALTY = 4


def load_slugs(path):
    if not os.path.exists(path):
        return []
    slugs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line and line.lower() not in EXCLUDE_SLUGS:
                slugs.append(line.lower())
    return sorted(set(slugs))


def load_seen(path):
    """URLs already reported as new by a previous run."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("urls", {})
    except (ValueError, OSError) as e:
        print(f"warning: could not read {path} ({e}); treating every "
              f"posting as new", file=sys.stderr)
        return {}


def save_seen(path, seen):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"urls": seen}, f, indent=0, sort_keys=True)
    os.replace(tmp, path)               # atomic, so a crash can't truncate it


def prune_file(path, dead_slugs):
    """Comment out newly-dead slugs, rewriting the file line by line.

    This used to rebuild the file from load_slugs(), which strips comments —
    so every previously pruned "# slug  (404)" line was silently deleted on
    the next --prune run, losing exactly the record the flag exists to keep.
    """
    dead_slugs = set(dead_slugs)
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            slug = line.split("#")[0].strip().lower()
            if slug and slug in dead_slugs:
                f.write(f"# {slug}  (404)\n")
            else:
                f.write(line if line.endswith("\n") else line + "\n")


def strip_html(text):
    # Unescape first, then strip tags. Greenhouse serves its job body
    # HTML-escaped (&lt;p&gt;), so stripping first left literal "<p>" and
    # "<span class=...>" sitting in the text the keyword matching reads.
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_matches(title):
    t = " " + title.lower() + " "
    if not any(k in t for k in TITLE_KEYWORDS):
        return False
    return not any(d in t for d in TITLE_DISQUALIFIERS)


def max_years_required(body):
    """Highest years-of-experience the posting asks for, 0 if unstated.

    For a range ("1-3 years") this returns the TOP of the range. The old code
    returned the bottom, so a 1-3 year role read as "1 year" — it survived the
    MAX_YEARS filter and then collected the largest years bonus in fit_score.
    """
    highest = 0
    body = DEGREE_PATTERN.sub(" ", body)
    for m in YEARS_PATTERN.finditer(body):
        try:
            low = int(m.group(1))
            high = int(m.group(2)) if m.group(2) else low
        except ValueError:
            continue
        if high <= 20:
            highest = max(highest, high)
    return highest


def find_flags(body, flags):
    low = body.lower()
    return [f for f in flags if f in low]


def is_bay_area(location):
    return any(c in (location or "").lower() for c in BAY_AREA)


def fit_score(title, location, body, years, crunch, board_size=0):
    """0-100ish. Higher = closer to what Blake actually wants."""
    t = title.lower()
    low = body.lower()
    score = 0

    # --- role type (max 25) ---
    for keywords, points in TITLE_PRIORITY:
        if any(k in t for k in keywords):
            score += points
            break

    # --- level signals (max ~35) ---
    if any(k in t for k in TITLE_NEWGRAD) or TITLE_LEVEL_ONE.search(title):
        score += 20                      # new-grad language in the title
    hits = sum(1 for k in EXPLICIT_NEWGRAD if k in low)
    score += min(hits * 6, 18)           # new-grad language in the body

    # --- years (max 15) ---
    if years == 0:
        score += 10                      # unstated is usually fine
    elif years == 1:
        score += 15
    elif years == 2:
        score += 6

    # --- location (max 15) ---
    if is_bay_area(location):
        score += 15
    elif "remote" in (location or "").lower():
        score += 4

    # --- mentorship (max 10, or penalty) ---
    if any(k in low for k in MENTOR_YOU):
        score += 10
    if any(k in low for k in MENTOR_OTHERS):
        score -= 12                      # implies they want a senior hire

    # --- company size (max ~18) ---
    if board_size:
        if board_size <= SMALL_BOARD:
            score += SMALL_BOARD_BONUS   # small board -> likely a startup
        elif board_size >= LARGE_BOARD:
            score -= LARGE_BOARD_PENALTY  # huge board -> very competitive
    if any(k in low for k in STARTUP_SIGNALS):
        score += 6

    # --- penalties ---
    score -= 15 * len(crunch)

    return max(score, 0)


def dedup_key(company, title, location):
    """Identifies the same JD listed twice.

    30 slugs currently sit in two slug files, so a company listed on both
    Greenhouse and Ashby returns the same posting under two different URLs
    that the run-to-run URL diff cannot collapse. Keyed on company + title +
    location rather than company alone, so a company with four genuinely
    different open roles still produces four rows.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return norm(company), norm(title), norm(location)


def make_row(company, source, title, location, url, body, board_size=0):
    years = max_years_required(body)
    if years > MAX_YEARS:
        return None
    if grad_year_too_far(title + " " + body):
        return None
    crunch = find_flags(body, CRUNCH_FLAGS)[:3]
    return {
        "fit_score": fit_score(title, location, body, years, crunch,
                               board_size),
        "company": company,
        "source": source,
        "title": title,
        "location": location,
        "bay_area": "yes" if is_bay_area(location) else "",
        "board_size": board_size or "",
        "years_stated": years or "",
        "good_signals": ", ".join(find_flags(body, GOOD_FLAGS)[:4]),
        "crunch_flags": ", ".join(crunch),
        "url": url,
    }


def fetch_greenhouse(slug, session):
    r = session.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        timeout=25)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    board_size = len(jobs)
    rows = []
    for job in jobs:
        title = job.get("title", "")
        if not title_matches(title):
            continue
        row = make_row(slug, "greenhouse", title,
                       (job.get("location") or {}).get("name", ""),
                       job.get("absolute_url", ""),
                       strip_html(job.get("content", "")), board_size)
        if row:
            rows.append(row)
    return rows


def fetch_ashby(slug, session):
    r = session.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        f"?includeCompensation=true", timeout=25)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    board_size = len(jobs)
    rows = []
    for job in jobs:
        title = job.get("title", "")
        if not title_matches(title):
            continue
        row = make_row(slug, "ashby", title, job.get("location", "") or "",
                       job.get("jobUrl", ""),
                       strip_html(job.get("descriptionPlain")
                                  or job.get("descriptionHtml", "")),
                       board_size)
        if row:
            rows.append(row)
    return rows


def fetch_lever(slug, session):
    r = session.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                    timeout=25)
    r.raise_for_status()
    jobs = r.json()
    board_size = len(jobs)
    rows = []
    for job in jobs:
        title = job.get("text", "")
        if not title_matches(title):
            continue
        body = strip_html(job.get("descriptionPlain")
                          or job.get("description", ""))
        for section in job.get("lists", []):
            body += " " + strip_html(section.get("content", ""))
        row = make_row(slug, "lever", title,
                       (job.get("categories") or {}).get("location", "") or "",
                       job.get("hostedUrl", ""), body, board_size)
        if row:
            rows.append(row)
    return rows


FETCHERS = {"greenhouse": fetch_greenhouse,
            "ashby": fetch_ashby,
            "lever": fetch_lever}


def scrape_one(source, slug, session, retries=2):
    """Fetch one board. Retries once on a transient failure.

    A board that times out returns no rows, which is indistinguishable from a
    board with no matching roles — so failures are counted and reported rather
    than swallowed. That matters for the run-to-run diff: a company that fails
    one week and succeeds the next looks like a pile of brand-new postings.
    """
    last = "error"
    for attempt in range(retries):
        try:
            return source, slug, FETCHERS[source](slug, session), "ok"
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 404:
                return source, slug, [], "404"
            last = f"http{code}"
            if code < 500 and code != 429:
                break                    # not worth retrying a 4xx
        except Exception as e:
            last = type(e).__name__
    return source, slug, [], last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="jobs.csv")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--prune", action="store_true",
                    help="comment out dead slugs in the slug files")
    ap.add_argument("--show-dupes", action="store_true",
                    help="list slugs that appear in more than one slug file")
    ap.add_argument("--seen", default="seen_urls.json",
                    help="store of URLs already reported (for the _new.csv)")
    ap.add_argument("--force-seen", action="store_true",
                    help="update the seen store even if boards failed")
    args = ap.parse_args()

    tasks = []
    by_source = {}
    for source, path in SLUG_FILES.items():
        slugs = load_slugs(path)
        if not slugs:
            print(f"note: {path} missing or empty", file=sys.stderr)
        by_source[source] = set(slugs)
        tasks += [(source, s) for s in slugs]

    if not tasks:
        print("No slugs found. Create the companies_*.txt files first.")
        return

    # A company lives on one platform, so a slug in two files is a mistake in
    # one of them. Harmless when the wrong one 404s, but when both resolve you
    # get the same company twice under two different URLs, which the
    # run-to-run URL diff can't collapse.
    dupes = set()
    sources = list(by_source)
    for i, a in enumerate(sources):
        for b in sources[i + 1:]:
            for slug in by_source[a] & by_source[b]:
                dupes.add((slug, a, b))
    if dupes:
        print(f"note: {len(dupes)} slugs appear in more than one slug file "
              f"(run --show-dupes to list them)", file=sys.stderr)

    print(f"Checking {len(tasks)} boards...\n")

    all_rows = []
    dead = {"greenhouse": [], "ashby": [], "lever": []}
    failed = []
    hits = 0

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (job search)"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(scrape_one, s, g, session) for s, g in tasks]
        for fut in as_completed(futures):
            source, slug, rows, status = fut.result()
            if status == "ok":
                all_rows.extend(rows)
                if rows:
                    hits += 1
                    print(f"  {slug} ({source}): {len(rows)}")
            elif status == "404":
                dead[source].append(slug)
            else:
                failed.append((source, slug, status))

    if args.prune:
        for source, path in SLUG_FILES.items():
            if not dead[source] or not os.path.exists(path):
                continue
            prune_file(path, dead[source])
            print(f"pruned {len(dead[source])} dead slugs from {path}")

    all_rows.sort(key=lambda r: (-r["fit_score"], r["company"], r["title"]))

    # Collapse the same JD listed twice (usually a slug sitting in two slug
    # files). Sorted by score first, so the copy we keep is the better-scoring
    # one; different roles at the same company keep their own rows.
    seen_jd, deduped = set(), []
    for r in all_rows:
        key = dedup_key(r["company"], r["title"], r["location"])
        if key in seen_jd:
            continue
        seen_jd.add(key)
        deduped.append(r)
    collapsed = len(all_rows) - len(deduped)
    all_rows = deduped

    fields = ["fit_score", "company", "source", "title", "location",
              "bay_area", "board_size", "years_stated", "good_signals",
              "crunch_flags", "url"]

    def write_csv(path, rows):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    write_csv(args.output, all_rows)

    # --- what's actually new since last run --------------------------------
    seen = load_seen(args.seen)
    new_rows = [r for r in all_rows if r["url"] not in seen]
    base, ext = os.path.splitext(args.output)
    new_path = f"{base}_new{ext or '.csv'}"
    write_csv(new_path, new_rows)

    # A run with failed boards is an incomplete picture. Recording those URLs
    # as "seen" would be fine, but NOT recording them matters: if we marked
    # this run complete, every posting from a board that failed today would
    # surface as brand new the moment it succeeds again.
    today = datetime.date.today().isoformat()
    if failed and not args.force_seen:
        print(f"\nNOT updating {args.seen}: {len(failed)} boards failed this "
              f"run, so it would poison the next diff. Re-run, or pass "
              f"--force-seen to record it anyway.")
    else:
        for r in all_rows:
            seen.setdefault(r["url"], today)
        save_seen(args.seen, seen)

    total_dead = sum(len(v) for v in dead.values())
    bay = sum(1 for r in all_rows if r["bay_area"] == "yes")
    good = sum(1 for r in all_rows if r["good_signals"])
    flagged = sum(1 for r in all_rows if r["crunch_flags"])

    strong = sum(1 for r in all_rows if r["fit_score"] >= 60)
    decent = sum(1 for r in all_rows if 40 <= r["fit_score"] < 60)

    print(f"\n{len(all_rows)} roles from {hits} companies -> {args.output}")
    print(f"  {len(new_rows)} NEW since last run -> {new_path}  <- read this one")
    if collapsed:
        print(f"  {collapsed} duplicate JDs collapsed (same role listed twice)")
    print(f"  {strong} strong fit (score 60+)  <- start here")
    print(f"  {decent} worth a look (40-59)")
    print(f"  {bay} Bay Area")
    print(f"  {good} with early-career or mentorship signals")
    print(f"  {flagged} flagged for crunch language")
    if total_dead:
        extra = "" if args.prune else "  (run --prune to comment them out)"
        print(f"  {total_dead} dead slugs{extra}")

    if failed:
        print(f"\n  !! {len(failed)} boards FAILED to fetch — these look "
              f"identical to 'no matching roles', so this run is an "
              f"incomplete picture:")
        for source, slug, status in sorted(failed)[:15]:
            print(f"       {slug} ({source}): {status}")
        if len(failed) > 15:
            print(f"       ... and {len(failed) - 15} more")
        print("     Re-run before treating this CSV as a diff baseline.")

    if dupes and args.show_dupes:
        print(f"\n  {len(dupes)} slugs in more than one slug file:")
        for slug, a, b in sorted(dupes):
            print(f"       {slug}: {a} + {b}")


if __name__ == "__main__":
    main()