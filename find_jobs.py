#!/usr/bin/env python3
"""
Pulls open roles from Greenhouse, Ashby, Lever, and Rippling job boards.

Company slugs live in plain text files so the list can grow without
editing this script.

Setup:
    pip install requests
    # companies_greenhouse.txt / companies_ashby.txt / companies_lever.txt
    # companies_rippling.txt
    # one slug per line; blank lines and # comments ignored

Usage:
    python find_jobs.py
    python find_jobs.py -o jobs.csv
    python find_jobs.py --workers 16     # faster
    python find_jobs.py --prune          # comment out dead slugs in the files
    python find_jobs.py --show-dupes     # slugs listed under >1 platform
    python find_jobs.py --seen PATH      # where the seen-URL store lives
    python find_jobs.py --force-seen     # record a run even if boards failed
    python find_jobs.py --grad-date 2026-05   # target graduation month
    python find_jobs.py --verbose        # full error detail on failed boards

Finding slugs — look at any job posting URL:
    job-boards.greenhouse.io/AIRBYTE/jobs/123  -> airbyte
    jobs.ashbyhq.com/CLICKHOUSE/abc            -> clickhouse
    jobs.lever.co/EXAMPLE/xyz                  -> example
    ats.rippling.com/EXAMPLE/jobs/<uuid>       -> example

Bulk-collect with Google:
    site:job-boards.greenhouse.io "data engineer" "san francisco"
    site:jobs.ashbyhq.com "new grad"
    site:jobs.lever.co "backend engineer"
    site:ats.rippling.com "backend engineer"
"""

import csv
import re
import json
import os
import sys
import html
import datetime
import argparse
from collections.abc import Iterable, Sequence
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

SLUG_FILES = {
    "greenhouse": "companies_greenhouse.txt",
    "ashby": "companies_ashby.txt",
    "lever": "companies_lever.txt",
    "rippling": "companies_rippling.txt",
}

# Lever's own demo boards. Both are full of generic postings that score well
# and are never a real lead, so they are skipped on every source.
EXCLUDE_SLUGS = {"leverdemo-8", "salesdemo-jr"}

TITLE_KEYWORDS = [
    "data engineer", "data platform", "data infrastructure",
    "backend", "back-end", "back end",
    "platform engineer", "infrastructure engineer", "software engineer",
    "solutions engineer", "solution engineer", "sales engineer",
    "forward deployed", "customer engineer",
    "application engineer", "applications engineer",
    "implementation engineer", "technical consultant",
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

# Job posts abbreviate as often as not ("New Grad (Dec 2026)"), so accept
# both forms. Longest alternatives first so "january" wins over "jan", and
# the forms are enumerated rather than using a "jan[a-z]*" wildcard, which
# would read "maybe 2027" as May.
_MONTH_ALTS = sorted(
    list(MONTHS) + [m[:3] + r"\.?" for m in MONTHS] + [r"sept\.?"],
    key=len, reverse=True)
MONTH_LOOKUP = {m[:3]: i for m, i in MONTHS.items()}

# An optional month followed by a plausible year.
DATE_PATTERN = re.compile(
    r"\b(?:(" + "|".join(_MONTH_ALTS) + r")\s+)?(20[2-9]\d)\b",
    re.IGNORECASE)

# Words that make a nearby year a graduation date rather than a copyright
# line or a "founded in 2019".
GRAD_CUE = re.compile(r"graduat|class of|new grad|commencement|degree by"
                      r"|diploma", re.IGNORECASE)

# Some boards (Roblox) mark the cohort as a bare bracketed year at the front
# of the title — "[2027] Software Engineer, Early Career" — with no
# graduation wording anywhere in the posting for GRAD_CUE to catch.
BRACKETED_YEAR = re.compile(r"^\s*\[(20[2-9]\d)\]")


# Target graduation, for spotting reqs aimed at a later cohort. Overridable
# with --grad-date, since its meaning drifts with the calendar.
GRAD_DATE = (2026, 5)                    # May 2026


def grad_dates(text: str) -> list:
    """(year, month) pairs that look like a required graduation date.

    month is None when the text named only a year, which matters downstream:
    "New Grad 2026" is the target cohort, "New Grad (December 2026)" is the
    one after it.
    """
    found = []
    lead = BRACKETED_YEAR.match(text)
    if lead:
        found.append((int(lead.group(1)), None))
    for m in DATE_PATTERN.finditer(text):
        window = text[max(0, m.start() - 60):m.end() + 40]
        if not GRAD_CUE.search(window):
            continue
        raw = m.group(1)
        month = MONTH_LOOKUP[raw.lower().rstrip(".")[:3]] if raw else None
        found.append((int(m.group(2)), month))
    return found


def grad_year_too_far(text: str,
                      today: Optional[datetime.date] = None) -> bool:
    """True if every graduation date named is beyond the horizon."""
    dates = grad_dates(text)
    if not dates:
        return False                     # no class year named -> keep it
    today = today or datetime.date.today()
    horizon_month = today.month + GRAD_HORIZON_MONTHS
    cutoff = (today.year + (horizon_month - 1) // 12,
              (horizon_month - 1) % 12 + 1)
    # a bare year reads as June, the usual spring commencement
    return min((y, m if m is not None else 6) for y, m in dates) > cutoff


def cohort_later_than_yours(text: str,
                            grad_date: Optional[tuple] = None) -> bool:
    """True if the earliest cohort named graduates after GRAD_DATE.

    Still worth applying to, since programs often take grads from the prior
    year, so these are penalised rather than filtered out.

    grad_date defaults to None, not GRAD_DATE: a default argument binds once
    at import, which would silently ignore --grad-date.
    """
    grad_date = grad_date or GRAD_DATE
    dates = grad_dates(text)
    if not dates:
        return False
    my_year, my_month = grad_date
    # a bare year equal to the target year is that cohort, not a later one
    keys = [(y, my_month if m is None else m) for y, m in dates]
    return min(keys) > (my_year, my_month)

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

# Same, but in the title — even stronger. Level-I titles are matched by
# TITLE_LEVEL_ONE below, not by a bare "i" substring: that caught every word
# starting with i ("Infrastructure", "AI Backend Engineer").
TITLE_NEWGRAD = [
    "new grad", "new graduate", "early career", "associate",
    "university graduate", "entry level", "entry-level", "junior", "graduate",
]

# Standalone roman numeral I, case-sensitive so it can't match inside "AI".
# The negative lookahead keeps it off "II"/"III".
TITLE_LEVEL_ONE = re.compile(r"\bI\b(?!I)")

# Role types, best first. Data is listed before platform so that
# "data platform engineer" scores as data rather than as platform.
TITLE_PRIORITY = [
    (["data engineer", "data platform", "data infrastructure",
      "analytics engineer"], 25),
    (["backend", "back-end", "back end"], 25),
    (["software engineer", "platform engineer",
      "infrastructure engineer"], 18),
    (["forward deployed", "deployed engineer", "customer engineer",
      "solutions engineer", "solution engineer", "solutions architect",
      "solutions consultant", "sales engineer", "technical consultant",
      "application engineer", "applications engineer",
      "implementation engineer", "integration engineer"], 12),
    (["support engineer", "technical support", "developer support"], 8),
]

# TITLE_KEYWORDS gates the filter, TITLE_PRIORITY scores the role type, and
# the two have to agree. A keyword with no tier scores 0 for role type and
# sinks to the bottom of the list, where it would never be noticed.
_UNTIERED = [k for k in TITLE_KEYWORDS
             if not any(p in k or k in p
                        for tier, _ in TITLE_PRIORITY for p in tier)]
assert not _UNTIERED, (
    f"TITLE_KEYWORDS entries with no TITLE_PRIORITY tier (they would pass the "
    f"filter and score 0 for role type): {_UNTIERED}")

# "will mentor others" implies they want a senior hire
MENTOR_OTHERS = [
    "mentor other", "mentor junior", "mentor teammates", "mentor the team",
    "mentoring other", "mentor engineers", "raise the bar", "mentor peers",
]

# "will be mentored" — the useful direction. Phrases have to name the
# mentoring relationship; "supported by" and "with guidance" were dropped
# because they match investor and benefits boilerplate.
MENTOR_YOU = [
    "mentorship", "you'll be mentored", "learn from experienced",
    "dedicated mentor", "guidance from senior", "coaching",
]

# Startup signals worth a bonus — a real startup with a team already in place.
STARTUP_SIGNALS = [
    "series a", "series b", "series c", "early-stage", "early stage",
    "wear many hats", "0 to 1", "0-to-1", "greenfield",
]

# Signals an engineering team too small to learn from. "founding engineer"
# and "employee #7" describe a team with nobody senior to be mentored by.
TOO_EARLY_SIGNALS = [
    "founding engineer", "founding team", "first engineer",
    "first engineering hire", "early employee", "employee #",
    "one of our first", "ground floor", "join us early", "pre-seed",
]

# --- scoring weights -------------------------------------------------------
# Named here rather than inlined into fit_score so the whole scoring model is
# auditable in one place. board_size is reported in the CSV but deliberately
# not scored: it tracks how many roles a company has open, not its size.
W_NEWGRAD_TITLE = 20        # new-grad language in the title
W_NEWGRAD_BODY_PER_HIT = 6  # per distinct EXPLICIT_NEWGRAD phrase in the body
W_NEWGRAD_BODY_CAP = 18
W_YEARS_UNSTATED = 10       # usually fine, but more ambiguous than a stated 1
W_YEARS_LOW = 15            # stated floor of 0 or 1; "0-2 years" counts here
W_YEARS_TWO = 6
W_BAY_AREA = 15
W_REMOTE = 4
W_MENTOR_YOU = 8            # deliberately light; the phrases are weak evidence
W_MENTOR_OTHERS = -12       # implies they want a senior hire
W_STARTUP = 6

TOO_EARLY_PENALTY = 8
LATER_COHORT_PENALTY = 10

# Crunch language is a demerit worth seeing, not one that should bury an
# otherwise good posting, so the penalty is capped. The CSV still records
# every flag found.
CRUNCH_PENALTY_EACH = 5
CRUNCH_PENALTY_MAX_FLAGS = 3

# Summary buckets, named so retuning them is a one-line change.
STRONG_FIT = 60
WORTH_A_LOOK = 45

# CSV column order.
CSV_FIELDS = ["fit_score", "company", "source", "title", "location",
              "bay_area", "board_size", "years_stated", "good_signals",
              "crunch_flags", "url"]


def load_slugs(path: str) -> list:
    if not os.path.exists(path):
        return []
    slugs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line and line.lower() not in EXCLUDE_SLUGS:
                slugs.append(line.lower())
    return sorted(set(slugs))


def load_seen(path: str) -> dict:
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


def atomic_write(path: str, write_fn) -> None:
    """Write via a temp file and rename, so a crash can't truncate the target.

    The slug files matter here: they are hand-curated input and the only
    record of which slugs are dead.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            write_fn(f)
        os.replace(tmp, path)           # atomic on POSIX
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)              # don't leave a partial .tmp behind
        raise


def save_seen(path: str, seen: dict) -> None:
    atomic_write(path, lambda f: json.dump({"urls": seen}, f, indent=0,
                                           sort_keys=True))


def prune_file(path: str, dead_slugs: Iterable[str]) -> None:
    """Comment out newly-dead slugs, rewriting the file line by line.

    Rewritten line by line rather than rebuilt from load_slugs(), which
    strips comments and would drop every previous "# slug  (404)" record.
    """
    dead_slugs = set(dead_slugs)
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    def write(f):
        for line in lines:
            slug = line.split("#")[0].strip().lower()
            if slug and slug in dead_slugs:
                f.write(f"# {slug}  (404)\n")
            else:
                f.write(line if line.endswith("\n") else line + "\n")

    atomic_write(path, write)


def strip_html(text: Optional[str]) -> str:
    # Unescape before stripping tags: Greenhouse serves its body
    # HTML-escaped, so the reverse order leaves literal "<p>" in the text.
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_matches(title: str) -> bool:
    t = " " + title.lower() + " "
    if not any(k in t for k in TITLE_KEYWORDS):
        return False
    return not any(d in t for d in TITLE_DISQUALIFIERS)


def years_required(body: str) -> tuple:
    """(floor, ceiling) years-of-experience the posting asks for.

    A range carries two facts, used for two different things. The filter
    screens on the ceiling, so "1-3 years" is a 3-year req. The score rewards
    the floor, so "0-2 years" reads as openness to someone with no experience
    rather than as a demand for two years.

    Both are 0 when the posting states nothing.
    """
    floor, ceiling = 0, 0
    body = DEGREE_PATTERN.sub(" ", body)
    for m in YEARS_PATTERN.finditer(body):
        try:
            low = int(m.group(1))
            high = int(m.group(2)) if m.group(2) else low
        except ValueError:
            continue
        if high <= 20 and high >= ceiling:
            # track the floor belonging to the most demanding requirement,
            # so "0-2 years ... 2+ years" screens and scores off the same one
            ceiling, floor = high, low
    return floor, ceiling


def max_years_required(body: str) -> int:
    """Ceiling only — what the MAX_YEARS filter screens on."""
    return years_required(body)[1]


def find_flags(body: str, flags: Sequence[str]) -> list:
    """Phrases from `flags` present in `body`, without double-counting.

    A shorter phrase contained in a longer match is dropped, so a body saying
    "new graduate" scores once rather than also matching "new grad".

    Order follows the caller's list so the columns stay stable.
    """
    low = (body or "").lower()
    hits = [f for f in flags if f in low]
    return [f for f in hits
            if not any(f != other and f in other for other in hits)]


def is_bay_area(location: Optional[str]) -> bool:
    return any(c in (location or "").lower() for c in BAY_AREA)


# Remote postings that name only a non-US region. Remote-from-the-Bay is
# worth keeping; remote-from-Poland is not, so it goes with the non-Bay rest.
REMOTE_NON_US = re.compile(
    r"emea|europe|united kingdom|\buk\b|london|india|apac|canada|germany"
    r"|france|spain|poland|portugal|brazil|australia|singapore|japan"
    r"|israel|dublin|ireland|netherlands|lat[ao]m|mexico|argentina"
    r"|colombia|philippines|nigeria|kenya|switzerland|sweden",
    re.IGNORECASE,
)
REMOTE_US = re.compile(
    r"\b(?:u\.?s\.?a?\.?|united states|americas|nationwide|anywhere)\b",
    re.IGNORECASE,
)


def location_ok(location: Optional[str]) -> bool:
    """Bay Area, or remote-from-the-Bay. Everything else is dropped."""
    loc = (location or "").lower()
    if is_bay_area(loc):
        return True
    if "remote" not in loc:
        return False
    if REMOTE_US.search(loc):
        return True                      # "Remote (United States | Canada)"
    return not REMOTE_NON_US.search(loc)  # unspecified remote is fine


def fit_score(title: str, location: str, body: str, years: tuple,
              crunch: Sequence[str], grad_date: Optional[tuple] = None) -> int:
    """0-100ish. Higher is a closer match to the target profile.

    `years` is the (floor, ceiling) pair from years_required. Both are needed:
    the ceiling is the only way to tell an unstated requirement (0, 0) from an
    explicit "0-2 years" (0, 2).

    `crunch` is the full list of matched flags; the penalty cap is applied
    here, not by the caller.
    """
    years_floor, years_ceiling = years
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
        score += W_NEWGRAD_TITLE
    hits = len(find_flags(body, EXPLICIT_NEWGRAD))
    score += min(hits * W_NEWGRAD_BODY_PER_HIT, W_NEWGRAD_BODY_CAP)

    # --- years (max 15) ---
    if years_ceiling == 0:
        score += W_YEARS_UNSTATED        # nothing stated anywhere in the body
    elif years_floor <= 1:
        score += W_YEARS_LOW             # "0-2 years" or "1 year"
    elif years_floor == 2:
        score += W_YEARS_TWO

    # --- location (max 15) ---
    if is_bay_area(location):
        score += W_BAY_AREA
    elif "remote" in (location or "").lower():
        score += W_REMOTE

    # --- mentorship ---
    # Weighted lightly: "mentorship" and "coaching" show up in boilerplate
    # benefits copy, so the phrase match is weak evidence on its own.
    if any(k in low for k in MENTOR_YOU):
        score += W_MENTOR_YOU
    if any(k in low for k in MENTOR_OTHERS):
        score += W_MENTOR_OTHERS         # implies they want a senior hire

    # --- company stage, from the posting text rather than the board size ---
    if any(k in low for k in STARTUP_SIGNALS):
        score += W_STARTUP
    if any(k in low for k in TOO_EARLY_SIGNALS):
        score -= TOO_EARLY_PENALTY

    # --- penalties ---
    score -= CRUNCH_PENALTY_EACH * min(len(crunch), CRUNCH_PENALTY_MAX_FLAGS)

    if cohort_later_than_yours(title + " " + body, grad_date):
        score -= LATER_COHORT_PENALTY

    return max(score, 0)


def dedup_key(company: str, title: str, location: str) -> tuple:
    """Identifies the same posting listed twice.

    A company on two platforms returns the same role under two URLs, which a
    URL diff cannot collapse. Keyed on company + title + location rather than
    company alone, so genuinely different roles keep their own rows.
    """
    def norm(s):                         # PEP 8 E731: def, not an assigned lambda
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    return norm(company), norm(title), norm(location)


def make_row(company: str, source: str, title: str, location: str, url: str,
             body: str, board_size: int = 0,
             grad_date: Optional[tuple] = None) -> Optional[dict]:
    years_floor, years_ceiling = years_required(body)
    if years_ceiling > MAX_YEARS:
        return None
    if grad_year_too_far(title + " " + body):
        return None
    if not location_ok(location):
        return None
    # Every flag, not a truncated subset: fit_score applies its own cap, and
    # truncating here would hide whether that cap was binding.
    crunch = find_flags(body, CRUNCH_FLAGS)
    return {
        "fit_score": fit_score(title, location, body,
                               (years_floor, years_ceiling), crunch,
                               grad_date),
        "company": company,
        "source": source,
        "title": title,
        "location": location,
        "bay_area": "yes" if is_bay_area(location) else "",
        "board_size": board_size or "",
        "years_stated": years_ceiling or "",
        "good_signals": ", ".join(find_flags(body, GOOD_FLAGS)[:4]),
        "crunch_flags": ", ".join(crunch),
        "url": url,
    }


def fetch_greenhouse(slug: str, session: requests.Session) -> list:
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


def fetch_ashby(slug: str, session: requests.Session) -> list:
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


def fetch_lever(slug: str, session: requests.Session) -> list:
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


RIPPLING_BOARD = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"


def _rippling_body(description) -> str:
    """Rippling splits the posting into {"company": html, "role": html}.

    Both halves are kept: "role" carries the requirements, "company" carries
    the culture copy CRUNCH_FLAGS looks for. Falls back to a plain string if
    the shape changes.
    """
    if isinstance(description, dict):
        return strip_html(" ".join(str(v) for v in description.values()))
    return strip_html(description or "")


def fetch_rippling(slug: str, session: requests.Session) -> list:
    """Rippling needs two requests per posting.

    Its board endpoint returns no description, so the body is fetched per job.
    The title filter runs against the board listing first so that only
    matching postings cost a second request.

    The location filter is NOT applied to the listing: it carries one location
    where the detail carries all of them, so a role listed in both Pittsburgh
    and the Bay Area would be dropped on the listing's single value.
    """
    r = session.get(RIPPLING_BOARD.format(slug=slug), timeout=25)
    r.raise_for_status()
    jobs = r.json()
    board_size = len(jobs)
    rows = []
    for job in jobs:
        title = job.get("name", "")
        if not title_matches(title):
            continue
        detail_url = f"{RIPPLING_BOARD.format(slug=slug)}/{job.get('uuid')}"
        d = session.get(detail_url, timeout=25)
        if d.status_code == 404:
            continue                     # posting pulled between the two calls
        # anything else is raised so scrape_one reports the board as failed:
        # a silently skipped posting looks like "no matching roles"
        d.raise_for_status()
        detail = d.json()
        locations = detail.get("workLocations") or []
        if not locations:
            single = (job.get("workLocation") or {}).get("label", "")
            locations = [single] if single else []
        row = make_row(slug, "rippling", title, ", ".join(locations),
                       job.get("url", ""),
                       _rippling_body(detail.get("description")), board_size)
        if row:
            rows.append(row)
    return rows


FETCHERS = {"greenhouse": fetch_greenhouse,
            "ashby": fetch_ashby,
            "lever": fetch_lever,
            "rippling": fetch_rippling}


def build_session(workers: int) -> requests.Session:
    """A session sized for the worker pool, with backoff on transient errors.

    The default urllib3 pool holds 10 connections, so running more workers
    than that churns connections. Retry adds exponential backoff and honours
    Retry-After, which matters for 429s.

    404 is deliberately not in status_forcelist: a dead slug is an expected
    result that scrape_one classifies, not a transient failure.
    """
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (job search)"
    retry = Retry(
        total=2,
        backoff_factor=0.5,              # 0.5s, 1.0s between attempts
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,           # let raise_for_status() classify
    )
    adapter = HTTPAdapter(max_retries=retry,
                          pool_maxsize=max(workers, 10),
                          pool_connections=max(workers, 10))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def scrape_one(source: str, slug: str, session: requests.Session) -> tuple:
    """Fetch one board and classify the outcome.

    Transient retries happen in the session adapter, so this is one logical
    attempt. Failures are reported rather than swallowed: a board that times
    out returns no rows, which otherwise looks identical to a board with no
    matching roles and would resurface as brand new next run.
    """
    try:
        return source, slug, FETCHERS[source](slug, session), "ok"
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        if code == 404:
            return source, slug, [], "404"
        return source, slug, [], f"http{code}"
    except Exception as e:
        # keep the message, not just the class name — a failed board
        # suppresses the seen-store update, so the reason matters
        return source, slug, [], f"{type(e).__name__}: {e}"


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Rank open new-grad and early-career engineering roles "
                    "from public Greenhouse, Ashby, Lever and Rippling "
                    "job-board APIs.")
    ap.add_argument("-o", "--output", default="jobs.csv")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--prune", action="store_true",
                    help="comment out dead slugs in the slug files")
    ap.add_argument("--show-dupes", action="store_true",
                    help="list slugs that appear in more than one slug file")
    ap.add_argument("--seen", default="seen_urls.json",
                    help="store of URLs already reported (for the _new.csv)")
    ap.add_argument("--force-seen", action="store_true",
                    help="update the seen store even if boards failed; this "
                         "poisons the next run's diff, so prefer re-running")
    ap.add_argument("--grad-date", metavar="YYYY-MM",
                    help="target graduation month, for spotting reqs aimed "
                         "at a later cohort (default "
                         f"{GRAD_DATE[0]}-{GRAD_DATE[1]:02d})")
    ap.add_argument("--verbose", action="store_true",
                    help="print full error detail for every failed board")
    return ap.parse_args(argv)


def parse_grad_date(text: str) -> tuple:
    """'YYYY-MM' -> (year, month). Raises SystemExit on malformed input."""
    m = re.fullmatch(r"(20\d{2})-(0[1-9]|1[0-2])", (text or "").strip())
    if not m:
        raise SystemExit(f"--grad-date must look like 2026-05, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def collect_tasks() -> tuple:
    """(tasks, slugs_by_source) from the three slug files."""
    tasks, by_source = [], {}
    for source, path in SLUG_FILES.items():
        slugs = load_slugs(path)
        if not slugs:
            print(f"note: {path} missing or empty", file=sys.stderr)
        by_source[source] = set(slugs)
        tasks += [(source, s) for s in slugs]
    return tasks, by_source


def find_cross_file_dupes(by_source: dict) -> set:
    """Slugs listed under more than one platform.

    A company lives on one platform, so a slug in two files is a mistake in
    one of them. Harmless when the wrong one 404s, but when both resolve you
    get the same company twice under two different URLs, which the run-to-run
    URL diff can't collapse.
    """
    dupes = set()
    sources = list(by_source)
    for i, a in enumerate(sources):
        for b in sources[i + 1:]:
            for slug in by_source[a] & by_source[b]:
                dupes.add((slug, a, b))
    return dupes


def run_scrape(tasks: Sequence[tuple], workers: int) -> tuple:
    """Fetch every board concurrently. -> (rows, dead_by_source, failed, hits)"""
    rows = []
    dead = {source: [] for source in SLUG_FILES}
    failed = []
    hits = 0

    session = build_session(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scrape_one, s, g, session) for s, g in tasks]
        for fut in as_completed(futures):
            source, slug, got, status = fut.result()
            if status == "ok":
                rows.extend(got)
                if got:
                    hits += 1
                    print(f"  {slug} ({source}): {len(got)}")
            elif status == "404":
                dead[source].append(slug)
            else:
                failed.append((source, slug, status))
    return rows, dead, failed, hits


def rank_and_dedup(rows: list) -> tuple:
    """Sort by fit, then collapse the same JD listed twice. -> (rows, n_collapsed)

    Sorted by score first, so the copy kept is the better-scoring one;
    different roles at the same company keep their own rows.
    """
    rows.sort(key=lambda r: (-r["fit_score"], r["company"], r["title"]))
    seen_jd, deduped = set(), []
    for r in rows:
        key = dedup_key(r["company"], r["title"], r["location"])
        if key in seen_jd:
            continue
        seen_jd.add(key)
        deduped.append(r)
    return deduped, len(rows) - len(deduped)


def write_csv(path: str, rows: Sequence[dict]) -> None:
    def write(f):
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)

    atomic_write(path, write)


def write_outputs(rows: list, output: str, seen_path: str, failed: list,
                  force_seen: bool) -> tuple:
    """Write the ranked CSV and the net-new CSV. -> (new_rows, new_path)"""
    write_csv(output, rows)

    seen = load_seen(seen_path)
    new_rows = [r for r in rows if r["url"] not in seen]
    base, ext = os.path.splitext(output)
    new_path = f"{base}_new{ext or '.csv'}"
    write_csv(new_path, new_rows)

    # A run with failed boards is an incomplete picture. Recording those URLs
    # as "seen" would be fine, but NOT recording them matters: if we marked
    # this run complete, every posting from a board that failed today would
    # surface as brand new the moment it succeeds again.
    if failed and not force_seen:
        print(f"\nNOT updating {seen_path}: {len(failed)} boards failed this "
              f"run, so it would poison the next diff. Re-run, or pass "
              f"--force-seen to record it anyway.")
    else:
        today = datetime.date.today().isoformat()
        for r in rows:
            seen.setdefault(r["url"], today)
        save_seen(seen_path, seen)
    return new_rows, new_path


def print_summary(rows: list, new_rows: list, new_path: str, output: str,
                  hits: int, collapsed: int, dead: dict, failed: list,
                  dupes: set, args: argparse.Namespace) -> None:
    total_dead = sum(len(v) for v in dead.values())
    bay = sum(1 for r in rows if r["bay_area"] == "yes")
    good = sum(1 for r in rows if r["good_signals"])
    flagged = sum(1 for r in rows if r["crunch_flags"])
    strong = sum(1 for r in rows if r["fit_score"] >= STRONG_FIT)
    decent = sum(1 for r in rows if WORTH_A_LOOK <= r["fit_score"] < STRONG_FIT)

    print(f"\n{len(rows)} roles from {hits} companies -> {output}")
    print(f"  {len(new_rows)} NEW since last run -> {new_path}  <- read this one")
    if collapsed:
        print(f"  {collapsed} duplicate JDs collapsed (same role listed twice)")
    print(f"  {strong} strong fit (score {STRONG_FIT}+)  <- start here")
    print(f"  {decent} worth a look ({WORTH_A_LOOK}-{STRONG_FIT - 1})")
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
        shown = sorted(failed) if args.verbose else sorted(failed)[:15]
        for source, slug, status in shown:
            print(f"       {slug} ({source}): {status}")
        if not args.verbose and len(failed) > 15:
            print(f"       ... and {len(failed) - 15} more (--verbose for all)")
        print("     Re-run before treating this CSV as a diff baseline.")

    if dupes and args.show_dupes:
        print(f"\n  {len(dupes)} slugs in more than one slug file:")
        for slug, a, b in sorted(dupes):
            print(f"       {slug}: {a} + {b}")


def main(argv=None) -> int:
    global GRAD_DATE
    args = parse_args(argv)
    if args.grad_date:
        GRAD_DATE = parse_grad_date(args.grad_date)

    tasks, by_source = collect_tasks()
    if not tasks:
        print("No slugs found. Create the companies_*.txt files first.")
        return 1

    dupes = find_cross_file_dupes(by_source)
    if dupes:
        print(f"note: {len(dupes)} slugs appear in more than one slug file "
              f"(run --show-dupes to list them)", file=sys.stderr)

    print(f"Checking {len(tasks)} boards...\n")
    rows, dead, failed, hits = run_scrape(tasks, args.workers)

    if args.prune:
        for source, path in SLUG_FILES.items():
            if not dead[source] or not os.path.exists(path):
                continue
            prune_file(path, dead[source])
            print(f"pruned {len(dead[source])} dead slugs from {path}")

    rows, collapsed = rank_and_dedup(rows)
    new_rows, new_path = write_outputs(rows, args.output, args.seen, failed,
                                       args.force_seen)
    print_summary(rows, new_rows, new_path, args.output, hits, collapsed,
                  dead, failed, dupes, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
