#!/usr/bin/env python3
"""
Pulls Bay Area early-career roles out of Hacker News "Who is hiring?" threads.

A thread goes up on the first weekday of each month and skews heavily SF and
startup. Posts are usually written by a founder or an engineer on the team, so
each row carries a named person to contact, which an ATS posting never does.

This is a separate script rather than a fifth fetcher in find_jobs.py because
a comment is freeform prose: company, title and location have to be parsed
heuristically instead of read from named JSON fields. Those rows stay out of
jobs.csv so its ranking is not diluted by parse errors.

Filters and scoring are imported from find_jobs.py, so a weight change there
applies to both.

Data source: the official free HN Algolia API (no key, no login).
    https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring
    https://hn.algolia.com/api/v1/items/<story_id>

Usage:
    python hn_hiring.py                    # newest thread -> hn_hiring.csv
    python hn_hiring.py --months 3         # last 3 threads
    python hn_hiring.py -o out.csv
    python hn_hiring.py --seen hn_seen.json
    python hn_hiring.py --all-locations    # skip the Bay-Area/remote filter
"""

import argparse
import csv
import datetime
import html
import os
import re
import sys
from collections import Counter

import requests

import find_jobs as fj

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{story_id}"

# whoishiring is the bot account that posts the monthly threads; tagging on the
# author is what keeps this from matching the many "Who is hiring" discussions.
HN_TAGS = "story,author_whoishiring"
HN_QUERY = "Ask HN: Who is hiring?"

# The convention is a pipe-delimited header line:
#   Company (YC S24) | Role | Location | ONSITE/REMOTE | url
# Field order varies in practice, so each field is tested rather than trusted
# by position.
FIELD_SEP = re.compile(r"\s*\|\s*")

# "Acme (YC S24)", "Acme (Series B)", "Acme, Inc." -> "Acme"
COMPANY_NOISE = re.compile(
    r"\s*\((?:yc\s*[swf]?\d{2,4}|series\s+[a-z]|seed|yc)\)|,\s*inc\.?$|,\s*llc$",
    re.IGNORECASE)

URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")

# HN renders a long link as <a href="FULL-URL">truncated-display-text...</a>,
# so the visible text is not the URL. Always read the href.
HREF = re.compile(r'<a\s[^>]*href="([^"]+)"', re.IGNORECASE)

# Prefer a link that points at a posting over a company homepage.
POSTING_URL = re.compile(
    r"job|career|hiring|greenhouse|ashby|lever|rippling|workable|boards",
    re.IGNORECASE)

# Many posts put the link inside the company field: "Acme ( https://acme.com )".
COMPANY_URL_TAIL = re.compile(r"\s*\(?\s*https?://\S*.*$", re.IGNORECASE)

# ONSITE / REMOTE / HYBRID / FULL-TIME land in whichever field the poster
# chose. They describe the arrangement, not the role.
MODALITY = re.compile(
    r"\b(?:onsite|on-site|remote(?:-first)?|hybrid|full[- ]time|part[- ]time"
    r"|intern(?:ship)?s?|visa|h1b|contract)\b[\s:,-]*", re.IGNORECASE)

# A real title is short; anything longer is the pitch running past the header,
# which would mis-score the row.
MAX_TITLE_LEN = 60

# Prose giveaways: if these survive in a candidate title it is a sentence.
TITLE_PROSE = re.compile(r"\b(?:i'?m|we'?re|we are|our|you'?ll|join us|is a|"
                         r"are a|looking for|hiring for)\b", re.IGNORECASE)

# A header line is the first chunk before a sentence break. Comments often run
# the pitch straight on after the header, so cut at the first period that is
# followed by a space and a capital, or just take the first 300 chars.
HEADER_CUT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

CSV_FIELDS = ["fit_score", "company", "source", "title", "location", "bay_area",
              "years_stated", "good_signals", "crunch_flags", "hn_author",
              "thread", "url", "comment_url"]


def fetch_threads(session: requests.Session, months: int) -> list:
    """The `months` most recent Who-is-hiring stories, newest first."""
    r = session.get(HN_SEARCH,
                    params={"query": HN_QUERY, "tags": HN_TAGS,
                            "hitsPerPage": max(months, 1)},
                    timeout=30)
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        title = h.get("title") or ""
        if "who is hiring" not in title.lower():
            continue
        out.append({"id": str(h["objectID"]), "title": title,
                    "created": (h.get("created_at") or "")[:10]})
    return out[:months]


def fetch_comments(session: requests.Session, story_id: str) -> list:
    """Top-level comments of one thread.

    Only top-level: replies are discussion ("is this remote?"), not postings.
    """
    r = session.get(HN_ITEM.format(story_id=story_id), timeout=60)
    r.raise_for_status()
    kids = r.json().get("children") or []
    return [k for k in kids if k.get("text")]


def header_fields(text: str) -> list:
    """The pipe-delimited fields of the comment's header line."""
    head = HEADER_CUT.split(text, 1)[0][:300]
    return [f.strip() for f in FIELD_SEP.split(head) if f.strip()]


def balance_brackets(s: str) -> str:
    """Drop empty bracket pairs and repair orphaned ones.

    Both cleaners cut text mid-field, at a URL or comma or modality marker,
    which routinely leaves a bracket without its pair.
    """
    s = re.sub(r"\(\s*\)", "", s)
    if s.count("(") > s.count(")"):
        s = s[:s.rindex("(")]
    while s.count(")") > s.count("("):
        s = s.replace(")", "", 1)
    return re.sub(r"\s+", " ", s).strip(" -–—:,|")


def clean_company(field: str) -> str:
    out = COMPANY_URL_TAIL.sub("", field)
    out = COMPANY_NOISE.sub("", out)
    return balance_brackets(out) or field.strip()


def clean_title(field: str) -> str:
    """Strip modality markers and leftover punctuation from a title field."""
    out = MODALITY.sub("", field)
    out = URL_IN_TEXT.sub("", out)
    out = re.sub(r"\bhttps?\b", "", out)      # remnant after a URL was split
    return balance_brackets(out)


def _usable_title(cand: str):
    """A cleaned title, or None if it reads as prose rather than a role."""
    cand = clean_title(cand)
    if not cand or len(cand) > MAX_TITLE_LEN:
        return None
    if TITLE_PROSE.search(cand):
        return None
    return cand if fj.title_matches(cand) else None


def pick_title(fields: list, text: str):
    """The field that reads as a role title, or None.

    A comment with no recognisable role is dropped rather than guessed at: a
    wrong title mis-scores the row. The length cap and prose check in
    _usable_title do most of the work, since headers often run into the pitch.
    """
    for f in fields[1:]:                 # fields[0] is the company
        got = _usable_title(f)
        if got:
            return got
    # Some posts list several roles in one field ("Backend, Frontend, Data
    # Eng") or run long enough to trip MAX_TITLE_LEN. Each field is split on
    # its own; joining them first would reintroduce the pipe separators.
    for f in fields[1:]:
        for part in re.split(r"[,/•]|\band\b", f):
            got = _usable_title(part)
            if got:
                return got
    return None


def pick_location(fields: list, text: str):
    """The field that reads as a location, or None."""
    for f in fields:
        if fj.is_bay_area(f):
            return f
    for f in fields:
        if "remote" in f.lower():
            return f
    # header didn't say; fall back to the first Bay Area city named anywhere
    low = text.lower()
    for city in fj.BAY_AREA:
        if city in low:
            return city.title()
    if "remote" in low:
        return "Remote"
    return None


def post_url(raw, text: str) -> str:
    """The best link in a comment, or "" if it has none."""
    hrefs = HREF.findall(html.unescape(raw or ""))
    if not hrefs:
        # no anchor tag: scan the plain text instead, trimming the punctuation
        # a URL picks up at the end of a sentence
        hrefs = [u.rstrip(".,;:)]") for u in URL_IN_TEXT.findall(text)]
    for u in hrefs:
        if POSTING_URL.search(u):
            return u
    return hrefs[0] if hrefs else ""


def make_row(comment: dict, thread: dict, all_locations: bool = False):
    text = fj.strip_html(comment.get("text"))
    if not text:
        return None
    fields = header_fields(text)
    if len(fields) < 2:
        return None                      # not the posting convention at all

    title = pick_title(fields, text)
    if not title:
        return None
    location = pick_location(fields, text) or ""
    if not all_locations and not fj.location_ok(location):
        return None

    years_floor, years_ceiling = fj.years_required(text)
    if years_ceiling > fj.MAX_YEARS:
        return None
    if fj.grad_year_too_far(title + " " + text):
        return None

    crunch = fj.find_flags(text, fj.CRUNCH_FLAGS)
    url = post_url(comment.get("text"), text)
    author = comment.get("author") or ""
    cid = comment.get("id")
    return {
        "fit_score": fj.fit_score(title, location, text,
                                  (years_floor, years_ceiling), crunch),
        "company": clean_company(fields[0]),
        "source": "hn",
        "title": title,
        "location": location,
        "bay_area": "yes" if fj.is_bay_area(location) else "",
        "years_stated": years_ceiling or "",
        "good_signals": ", ".join(fj.find_flags(text, fj.GOOD_FLAGS)[:4]),
        "crunch_flags": ", ".join(crunch),
        "hn_author": author,             # the contact — why this source exists
        "thread": thread.get("created", ""),
        "url": url,
        "comment_url": f"https://news.ycombinator.com/item?id={cid}",
    }


def write_csv(path: str, rows) -> None:
    def write(f):
        # QUOTE_ALL because url and comment_url sit next to each other: with
        # bare fields the row reads ",https://a,https://b" and an editor's link
        # detector swallows the comma, gluing the two into one dead link.
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)

    fj.atomic_write(path, write)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Bay Area early-career roles from HN Who-is-hiring threads.")
    ap.add_argument("-o", "--output", default="hn_hiring.csv")
    ap.add_argument("--months", type=int, default=1,
                    help="how many recent monthly threads to read (default 1)")
    ap.add_argument("--seen", default="hn_seen.json",
                    help="store of comment URLs already reported")
    ap.add_argument("--all-locations", action="store_true",
                    help="skip the Bay-Area/remote filter")
    ap.add_argument("--force-seen", action="store_true",
                    help="update the seen store even if a thread failed")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    session = fj.build_session(4)

    try:
        threads = fetch_threads(session, args.months)
    except Exception as e:
        print(f"could not list threads: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    if not threads:
        print("no Who-is-hiring threads found", file=sys.stderr)
        return 1

    rows, failed, scanned = [], [], 0
    for t in threads:
        try:
            comments = fetch_comments(session, t["id"])
        except Exception as e:
            failed.append((t["created"], f"{type(e).__name__}: {e}"))
            continue
        scanned += len(comments)
        print(f"  {t['created']}: {len(comments)} posts")
        for c in comments:
            row = make_row(c, t, args.all_locations)
            if row:
                rows.append(row)

    rows.sort(key=lambda r: (-r["fit_score"], r["company"], r["title"]))

    # same JD posted in consecutive months -> keep the better-scoring copy
    seen_jd, deduped = set(), []
    for r in rows:
        k = fj.dedup_key(r["company"], r["title"], r["location"])
        if k in seen_jd:
            continue
        seen_jd.add(k)
        deduped.append(r)
    collapsed = len(rows) - len(deduped)
    rows = deduped

    write_csv(args.output, rows)

    seen = fj.load_seen(args.seen)
    new_rows = [r for r in rows if r["comment_url"] not in seen]
    base, ext = os.path.splitext(args.output)
    new_path = f"{base}_new{ext or '.csv'}"
    write_csv(new_path, new_rows)

    if failed and not args.force_seen:
        print(f"\nNOT updating {args.seen}: {len(failed)} threads failed")
    else:
        today = datetime.date.today().isoformat()
        for r in rows:
            seen.setdefault(r["comment_url"], today)
        fj.save_seen(args.seen, seen)

    strong = sum(1 for r in rows if r["fit_score"] >= fj.STRONG_FIT)
    decent = sum(1 for r in rows
                 if fj.WORTH_A_LOOK <= r["fit_score"] < fj.STRONG_FIT)
    bay = sum(1 for r in rows if r["bay_area"] == "yes")
    print(f"\n{len(rows)} roles from {scanned} posts across "
          f"{len(threads)} thread(s) -> {args.output}")
    print(f"  {len(new_rows)} NEW since last run -> {new_path}  <- read this one")
    if collapsed:
        print(f"  {collapsed} reposts collapsed (same role in another month)")
    print(f"  {strong} strong fit ({fj.STRONG_FIT}+)  <- start here")
    print(f"  {decent} worth a look ({fj.WORTH_A_LOOK}-{fj.STRONG_FIT - 1})")
    print(f"  {bay} Bay Area")
    print("  every row has an hn_author to contact directly")
    for src, n in Counter(r["thread"] for r in rows).most_common():
        print(f"    {src}: {n}")
    if failed:
        print(f"\n  !! {len(failed)} threads failed:")
        for when, why in failed:
            print(f"       {when}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
