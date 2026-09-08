"""Regression tests for bugs that have already shipped once.

Every test here corresponds to a bug documented in CLAUDE.md under "Bugs
already found and fixed". They exist so that the fix is enforced by an
assertion rather than by a paragraph of prose that nothing executes.

Two of these bugs corrupted the only region of the output that matters:
the `" i"` substring bug affected 16% of all rows, and the YEARS_PATTERN
bug put over-experienced roles in 24% of the top 50.
"""

import datetime
import importlib.util
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("find_jobs",
                                                  _ROOT / "find_jobs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fj = _load()


# --- bug: TITLE_NEWGRAD contained the bare substrings "i " and " i" ---------
# They matched any word starting with i, so "Software Engineer, Infrastructure"
# and "AI Backend Engineer" took the full +20 new-grad title bonus.

@pytest.mark.parametrize("title", [
    "Software Engineer, Infrastructure",
    "AI Backend Engineer",
    "Data Engineer, Insights",
    "Backend Engineer II",
    "Software Engineer III",
])
def test_level_one_does_not_match_words_starting_with_i(title):
    assert not fj.TITLE_LEVEL_ONE.search(title), (
        f"{title!r} must not read as a level-I role")


@pytest.mark.parametrize("title", [
    "Software Engineer I",
    "Data Engineer I",
])
def test_level_one_still_matches_a_real_level_one_title(title):
    assert fj.TITLE_LEVEL_ONE.search(title)


# --- bug: YEARS_PATTERN let 20.6% of over-experienced roles through --------
# Four separate causes, one test each.

def test_years_gap_between_years_and_experience_is_wide_enough():
    # the old pattern allowed only [\s\w,] in the gap, so "of professional
    # software engineering experience" broke the match entirely
    body = "5+ years of professional software engineering experience"
    assert fj.max_years_required(body) == 5


def test_years_range_reads_the_top_not_the_bottom():
    # "1-3 years" parsed as 1, survived the MAX_YEARS filter, and then
    # collected the largest years bonus in the score
    assert fj.max_years_required("1-3 years of experience") == 3
    assert fj.max_years_required("3 to 5 years of experience") == 5


def test_years_matches_en_dash_and_em_dash_ranges():
    assert fj.max_years_required("3–5 years of experience") == 5
    assert fj.max_years_required("3—5 years of experience") == 5


def test_years_matches_requirements_that_never_say_experience():
    assert fj.max_years_required("3+ years building product features") == 3
    assert fj.max_years_required("6+ years in infrastructure") == 6


def test_degree_requirement_is_not_read_as_experience():
    body = "BS from a 4 year accredited university, 2+ years experience"
    assert fj.max_years_required(body) == 2


def test_unstated_years_is_zero():
    assert fj.max_years_required("We want someone great.") == 0


# --- bug: strip_html stripped tags before unescaping entities --------------
# Greenhouse serves its body HTML-escaped, so stripping first left literal
# "<p>" sitting in the text that keyword matching reads.

def test_strip_html_unescapes_before_stripping_tags():
    assert fj.strip_html("&lt;p&gt;New grad role&lt;/p&gt;") == "New grad role"
    assert "<" not in fj.strip_html("&lt;span class=x&gt;hi&lt;/span&gt;")


def test_strip_html_handles_plain_tags_and_none():
    assert fj.strip_html("<p>hello   world</p>") == "hello world"
    assert fj.strip_html(None) == ""
    assert fj.strip_html("") == ""


# --- bug: --prune rebuilt slug files from load_slugs(), which strips -------
# comments, silently deleting every prior "# slug  (404)" record.

def test_prune_preserves_previously_pruned_records(tmp_path):
    f = tmp_path / "companies_test.txt"
    f.write_text("# oldslug  (404)\nalive\ndeadslug\n")
    fj.prune_file(str(f), ["deadslug"])
    out = f.read_text()
    assert "# oldslug  (404)" in out, "prior 404 record was deleted"
    assert "# deadslug  (404)" in out
    assert "alive" in out


def test_prune_is_idempotent(tmp_path):
    f = tmp_path / "companies_test.txt"
    f.write_text("alive\ndeadslug\n")
    fj.prune_file(str(f), ["deadslug"])
    first = f.read_text()
    fj.prune_file(str(f), ["deadslug"])
    assert f.read_text() == first


# --- bug: leverdemo-8 is Lever's own demo board, not a company -------------

def test_leverdemo_is_excluded(tmp_path):
    assert "leverdemo-8" in fj.EXCLUDE_SLUGS
    f = tmp_path / "companies_lever.txt"
    f.write_text("leverdemo-8\nrealcompany\n")
    assert fj.load_slugs(str(f)) == ["realcompany"]


def test_load_slugs_strips_comments_and_blanks(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("alpha\n\n# dead  (404)\nBETA  # note\n")
    assert fj.load_slugs(str(f)) == ["alpha", "beta"]


# --- filter contract ------------------------------------------------------

def test_max_years_filter_boundary():
    assert fj.MAX_YEARS == 2


@pytest.mark.parametrize("title,expected", [
    ("Senior Software Engineer", False),
    ("Staff Data Engineer", False),
    ("Engineering Manager", False),
    ("Software Engineer III", False),
    ("Software Engineering Intern", False),
    ("Backend Engineer", True),
    ("Data Engineer, New Grad", True),
    ("Forward Deployed Engineer", True),
    # " ii" is deliberately NOT a disqualifier: level-II roles still pass,
    # they just no longer collect a new-grad bonus.
    ("Software Engineer II", True),
])
def test_title_filter(title, expected):
    assert fj.title_matches(title) is expected


@pytest.mark.parametrize("loc,expected", [
    ("San Francisco, CA", True),
    ("Palo Alto", True),
    ("Remote", True),
    ("Remote (United States)", True),
    ("Remote - US", True),
    ("Remote - Poland", False),
    ("Remote, EMEA", False),
    ("London, UK", False),
    ("New York, NY", False),
    ("", False),
    (None, False),
])
def test_location_filter(loc, expected):
    assert fj.location_ok(loc) is expected


# --- graduation horizon ---------------------------------------------------

def test_grad_year_kept_when_no_class_year_named():
    assert fj.grad_year_too_far("Backend engineer, great team") is False


def test_grad_year_drops_a_cohort_beyond_the_horizon():
    today = datetime.date(2026, 9, 1)
    assert fj.grad_year_too_far("graduating in May 2028", today) is True


def test_grad_year_keeps_a_winter_grad_inside_the_horizon():
    today = datetime.date(2026, 9, 1)
    assert fj.grad_year_too_far("graduating December 2026", today) is False


def test_grad_cue_required_so_copyright_years_are_ignored():
    today = datetime.date(2026, 9, 1)
    assert fj.grad_year_too_far("Founded in 2019. Copyright 2030.",
                                today) is False


def test_bracketed_cohort_year_in_title_is_read():
    assert fj.grad_dates("[2027] Software Engineer, Early Career") == [
        (2027, None)]


def test_month_abbreviations_do_not_swallow_other_words():
    # "maybe 2027" must not read as May 2027
    assert fj.grad_dates("graduating maybe 2027") == [(2027, None)]
