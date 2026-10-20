#!/usr/bin/env python3
"""
One command to run at the start of the week.

    python weekly.py

Four steps, in order:

  1. archive last week's output into jobs/ so it is never overwritten
  2. discover new companies and append them to the slug files
  3. sweep every ATS board (find_jobs.py)
  4. read the newest Hacker News "Who is hiring?" thread (hn_hiring.py)

Then it merges the two new-roles CSVs into one ranked file, weekly_new.csv,
which is the only thing that needs reading.

Discovery works off the HN threads rather than a search engine. Posters link
their own board, so a thread is full of ATS URLs: jobs.ashbyhq.com/<slug>,
job-boards.greenhouse.io/<slug>, jobs.lever.co/<slug>, ats.rippling.com/<slug>
and grnh.se shortlinks that redirect to one. Every slug is checked against the
platform API and only live boards with at least one posting are added, so the
slug files never fill up with guesses.

Usage:
    python weekly.py
    python weekly.py --dry-run             # show what would be added, write nothing
    python weekly.py --discover-months 6   # scan further back for new companies
    python weekly.py --no-discover         # just run both scrapers
    python weekly.py --no-archive
"""

import argparse
import csv
import datetime
import html
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import find_jobs as fj
import hn_hiring as hn

ARCHIVE_DIR = "jobs"
MERGED_OUTPUT = "weekly_new.csv"

# Board URLs as posters write them in a comment. The capture group is the slug.
ATS_PATTERNS = {
    "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9._-]*)",
    "greenhouse": (r"(?:job-)?boards\.greenhouse\.io/"
                   r"(?:embed/job_board\?for=)?([A-Za-z0-9][A-Za-z0-9._-]*)"),
    "lever": r"jobs\.lever\.co/([A-Za-z0-9][A-Za-z0-9._-]*)",
    "rippling": (r"ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                 r"([A-Za-z0-9][A-Za-z0-9._-]*)"),
}

HN_SEARCH = "https://hn.algolia.com/api/v1/search"

# ATS board hostnames, as search terms.
SEARCH_DOMAINS = ["jobs.ashbyhq.com", "job-boards.greenhouse.io",
                  "boards.greenhouse.io", "jobs.lever.co", "ats.rippling.com"]

# Greenhouse hands out grnh.se short links, which redirect to the real board.
SHORTLINK = re.compile(r"https?://grnh\.se/\w+", re.IGNORECASE)

# Slugs that are a platform's own page rather than a company board.
NOT_A_COMPANY = {"embed", "job_board", "jobs", "api", "www", "search", "apply"}

BOARD_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "rippling": ("https://api.rippling.com/platform/api/ats/v1/"
                 "board/{slug}/jobs"),
}

# Union of the two sources' columns. The two URL columns are kept apart, and
# the writer quotes every field, so neither gets glued to its neighbour by an
# editor's link detector.
MERGED_FIELDS = ["fit_score", "source", "company", "title", "location",
                 "bay_area", "years_stated", "good_signals", "crunch_flags",
                 "url", "hn_author", "thread", "board_size", "comment_url"]


def all_tracked() -> set:
    """Every slug already known, on any platform, live or pruned as dead.

    Deduping per-platform is not enough: adding a slug to one file when it is
    already active in another produces a cross-file duplicate, which is how
    most of the existing "(404)" entries were created in the first place.
    """
    known = set(fj.EXCLUDE_SLUGS)
    for slugs in tracked_slugs().values():
        known |= slugs
    return known


def tracked_slugs() -> dict:
    """Every slug already in a file, live or commented out as dead."""
    out = {}
    for source, path in fj.SLUG_FILES.items():
        known = set(fj.load_slugs(path))
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("#") and "(404)" in line:
                        known.add(line.split("#")[1].split("(")[0]
                                  .strip().lower())
        out[source] = known
    return out


def slugs_from_text(blob: str) -> dict:
    """Board slugs mentioned anywhere in a blob of text, by platform."""
    found = {source: set() for source in ATS_PATTERNS}
    for source, pattern in ATS_PATTERNS.items():
        for m in re.finditer(pattern, blob, re.IGNORECASE):
            slug = m.group(1).lower().rstrip(".")
            if slug not in NOT_A_COMPANY:
                found[source].add(slug)
    return found


def search_hn_links(session, pages: int = 3) -> str:
    """Text of every HN item that links an ATS board, via Algolia search.

    The hiring threads are only part of the picture: a board gets linked in
    Show HN posts, launch announcements and ordinary comments too. Searching
    all of HN for the ATS domains roughly doubles what the threads alone find.
    """
    texts = []
    for domain in SEARCH_DOMAINS:
        for page in range(pages):
            try:
                r = session.get(HN_SEARCH, params={"query": domain,
                                                   "hitsPerPage": 100,
                                                   "page": page}, timeout=30)
                if r.status_code != 200:
                    break
                payload = r.json()
            except Exception:
                break
            for hit in payload.get("hits", []):
                for key in ("comment_text", "story_text", "url", "title"):
                    if hit.get(key):
                        texts.append(html.unescape(str(hit[key])))
            if page + 1 >= payload.get("nbPages", 1):
                break
    return " ".join(texts)


def harvest_slugs(session, months: int, wide: bool = True) -> tuple:
    """Board slugs linked from HN. Returns (slugs_by_platform, n_sources)."""
    texts = []
    for thread in hn.fetch_threads(session, months):
        for comment in hn.fetch_comments(session, thread["id"]):
            texts.append(html.unescape(comment.get("text") or ""))
    n_comments = len(texts)
    if wide:
        texts.append(search_hn_links(session))
    blob = " ".join(texts)

    found = slugs_from_text(blob)

    links = set(SHORTLINK.findall(blob))
    if links:
        pattern = ATS_PATTERNS["greenhouse"]

        def resolve(url):
            try:
                r = session.get(url, timeout=15, allow_redirects=True)
                m = re.search(pattern, r.url, re.IGNORECASE)
                return m.group(1).lower() if m else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            for slug in pool.map(resolve, sorted(links)):
                if slug and slug not in NOT_A_COMPANY:
                    found["greenhouse"].add(slug)
    return found, n_comments


def verify_boards(session, candidates: dict, workers: int = 10) -> dict:
    """Keep only slugs whose board resolves and has at least one posting."""
    def check(task):
        source, slug = task
        try:
            r = session.get(BOARD_API[source].format(slug=slug), timeout=20)
            if r.status_code != 200:
                return None
            payload = r.json()
            jobs = payload if isinstance(payload, list) \
                else payload.get("jobs", [])
            return (source, slug, len(jobs)) if jobs else None
        except Exception:
            return None

    tasks = [(s, sl) for s, slugs in candidates.items() for sl in slugs]
    live = {source: [] for source in candidates}
    if not tasks:
        return live
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for got in pool.map(check, tasks):
            if got:
                source, slug, count = got
                live[source].append((slug, count))
    for rows in live.values():
        rows.sort()
    return live


def append_slugs(live: dict, stamp: str) -> int:
    """Append verified slugs under a dated header. Returns how many were added."""
    added = 0
    for source, rows in live.items():
        if not rows:
            continue
        path = fj.SLUG_FILES[source]
        body = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"\n# added {stamp} from HN hiring threads\n"
        body += "".join(f"{slug}\n" for slug, _ in rows)
        fj.atomic_write(path, lambda f, b=body: f.write(b))
        added += len(rows)
    return added


def archive_outputs(stamp: str) -> list:
    """Copy this week's output files into jobs/ before they are overwritten."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    saved = []
    for name in ("jobs.csv", "jobs_new.csv", "hn_hiring.csv",
                 "hn_hiring_new.csv", MERGED_OUTPUT):
        if not os.path.exists(name):
            continue
        base, ext = os.path.splitext(name)
        target = os.path.join(ARCHIVE_DIR, f"{base}_{stamp}{ext}")
        if os.path.exists(target):          # already archived today
            continue
        shutil.copy2(name, target)
        saved.append(target)
    return saved


def merge_new(paths, output: str) -> list:
    """Combine the per-source new-role CSVs into one ranked file."""
    rows = []
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append({k: row.get(k, "") for k in MERGED_FIELDS})
    rows.sort(key=lambda r: (-int(r["fit_score"] or 0), r["company"],
                             r["title"]))

    def write(f):
        w = csv.DictWriter(f, fieldnames=MERGED_FIELDS,
                           quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)

    fj.atomic_write(output, write)
    return rows


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Weekly run: add companies, sweep every board, read the "
                    "newest HN hiring thread, and merge what is new.")
    ap.add_argument("--discover-months", type=int, default=3,
                    help="HN threads to scan for new company boards (default 3)")
    ap.add_argument("--hn-months", type=int, default=1,
                    help="HN threads to read for roles (default 1)")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--no-discover", action="store_true",
                    help="skip company discovery")
    ap.add_argument("--no-wide", action="store_true",
                    help="only scan the hiring threads, skip the HN-wide "
                         "search for ATS links")
    ap.add_argument("--no-archive", action="store_true",
                    help="skip archiving last week's output")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what discovery would add, then stop")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    stamp = datetime.date.today().isoformat()
    session = fj.build_session(args.workers)

    print(f"=== weekly run {stamp} ===\n")

    if not args.no_archive and not args.dry_run:
        saved = archive_outputs(stamp)
        print(f"[1/4] archived {len(saved)} file(s) into {ARCHIVE_DIR}/")
    else:
        print("[1/4] archive skipped")

    added = 0
    if args.no_discover:
        print("[2/4] discovery skipped")
    else:
        print(f"[2/4] scanning {args.discover_months} HN thread(s) "
              f"for new company boards...")
        found, n_comments = harvest_slugs(session, args.discover_months,
                                          wide=not args.no_wide)
        known = all_tracked()
        candidates = {s: sorted(sl for sl in slugs if sl not in known)
                      for s, slugs in found.items()}
        n_cand = sum(len(v) for v in candidates.values())
        scope = "threads + all of HN" if not args.no_wide else "threads only"
        print(f"      {n_comments} thread comments ({scope}), "
              f"{sum(len(v) for v in found.values())} board links, "
              f"{n_cand} not already tracked")
        live = verify_boards(session, candidates, args.workers)
        for source, rows in sorted(live.items()):
            if rows:
                print(f"      {source:11} +{len(rows)}: "
                      f"{', '.join(s for s, _ in rows[:6])}"
                      f"{' ...' if len(rows) > 6 else ''}")
        if args.dry_run:
            print(f"\ndry run: {sum(len(v) for v in live.values())} slugs "
                  f"would be added; nothing written")
            return 0
        added = append_slugs(live, stamp)
        total = sum(len(fj.load_slugs(p)) for p in fj.SLUG_FILES.values())
        print(f"      added {added} companies -> {total} tracked")

    print("\n[3/4] sweeping ATS boards")
    if fj.main(["--workers", str(args.workers)]) != 0:
        print("ATS sweep failed", file=sys.stderr)
        return 1

    print("\n[4/4] reading HN hiring thread(s)")
    if hn.main(["--months", str(args.hn_months)]) != 0:
        print("HN read failed", file=sys.stderr)
        return 1

    rows = merge_new(["jobs_new.csv", "hn_hiring_new.csv"], MERGED_OUTPUT)
    strong = sum(1 for r in rows if int(r["fit_score"] or 0) >= fj.STRONG_FIT)
    decent = sum(1 for r in rows if fj.WORTH_A_LOOK
                 <= int(r["fit_score"] or 0) < fj.STRONG_FIT)
    bay = sum(1 for r in rows if r["bay_area"] == "yes")
    by_source = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1

    print(f"\n=== {len(rows)} new roles -> {MERGED_OUTPUT}  <- read this one ===")
    print(f"  {strong} strong fit ({fj.STRONG_FIT}+)")
    print(f"  {decent} worth a look ({fj.WORTH_A_LOOK}-{fj.STRONG_FIT - 1})")
    print(f"  {bay} Bay Area")
    if added:
        print(f"  {added} companies added to the slug files this run")
    for source, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"    {source}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
