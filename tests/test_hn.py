"""Tests for the HN Who-is-hiring source.

The comment bodies here are trimmed from real October 2026 thread posts, so the
parser is tested against the actual (inconsistent) community convention rather
than an idealised format.

The parse is heuristic, which is why this source writes its own CSV instead of
feeding jobs.csv. These tests pin the two behaviours that keep it honest: a
comment with no recognisable role is dropped rather than guessed at, and the
location filter is the one find_jobs.py uses.
"""

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import find_jobs as fj          # noqa: E402
import hn_hiring as hn          # noqa: E402

THREAD = {"id": "49922569", "created": "2026-10-01",
          "title": "Ask HN: Who is hiring? (October 2026)"}


def comment(text, author="someone", cid=1):
    return {"text": text, "author": author, "id": cid}


# --- header parsing -------------------------------------------------------

def test_header_fields_splits_on_pipes():
    f = hn.header_fields("Acme | Backend Engineer | San Francisco | FULL-TIME")
    assert f == ["Acme", "Backend Engineer", "San Francisco", "FULL-TIME"]


def test_header_stops_at_the_pitch():
    """The header runs straight into prose in most real posts."""
    text = ("Thunder Compute | Backend Engineer | San Francisco (Onsite). "
            "We are building a GPU cloud and we want you.")
    assert hn.header_fields(text)[0] == "Thunder Compute"
    assert not any("GPU cloud" in f for f in hn.header_fields(text))


@pytest.mark.parametrize("raw,clean", [
    ("Thunder Compute (YC S24)", "Thunder Compute"),
    ("Shepherd (Series B)", "Shepherd"),
    ("Acme (YC W21)", "Acme"),
    ("Acme, Inc.", "Acme"),
    ("Plain Co", "Plain Co"),
    # the link often sits inside the company field
    ("Column ( https://columntax.com )", "Column"),
    ("Typify.dev ( https://typify.dev )", "Typify.dev"),
    ("VLM Run ( https://vlm.run", "VLM Run"),
])
def test_clean_company_strips_funding_noise_and_urls(raw, clean):
    assert hn.clean_company(raw) == clean


@pytest.mark.parametrize("raw,clean", [
    ("ONSITE Backend Engineer", "Backend Engineer"),
    ("REMOTE | Data Engineer", "Data Engineer"),
    ("Backend Engineer (Full-time)", "Backend Engineer"),
    ("HYBRID Software Engineer", "Software Engineer"),
])
def test_clean_title_strips_modality_markers(raw, clean):
    assert hn.clean_title(raw) == clean


def test_prose_masquerading_as_a_title_is_rejected():
    """Posts that run the pitch straight into the header would otherwise use
    the whole sentence as the title."""
    f = ["Acme", "ONSITE I'm part of Acme's Applied ML team and we "
         "are hiring a software engineer", "Menlo Park, CA"]
    assert hn.pick_title(f, " | ".join(f)) is None


def test_an_overlong_title_field_is_rejected():
    long_role = "Backend Engineer " + "x" * hn.MAX_TITLE_LEN
    f = ["Acme", long_role, "San Francisco"]
    assert hn.pick_title(f, " | ".join(f)) is None


def test_modality_prefixed_title_still_matches():
    f = ["Acme", "ONSITE Backend Engineer", "San Francisco"]
    assert hn.pick_title(f, " | ".join(f)) == "Backend Engineer"


# --- title selection ------------------------------------------------------

def test_picks_the_field_that_reads_as_a_role():
    f = ["Acme", "San Francisco", "Backend Engineer", "REMOTE"]
    assert hn.pick_title(f, " | ".join(f)) == "Backend Engineer"


def test_a_multi_role_field_is_matched_on_the_role_it_contains():
    f = ["Acme", "Frontend, Data Engineer, Design", "San Francisco"]
    assert "Data Engineer" in hn.pick_title(f, " | ".join(f))


def test_an_overlong_multi_role_field_is_split_per_field():
    """A role field over the length cap falls to the split path, which must
    not rejoin the other fields."""
    f = ["New Lantern", "https://newlantern.ai",
         "Software engineers (backend and generalist product engineers, "
         "founding recruiter)", "Full time in San Francisco, CA"]
    got = hn.pick_title(f, " | ".join(f))
    assert got == "Software engineers"
    assert "|" not in got and "newlantern" not in got


def test_company_name_is_never_used_as_the_title():
    """fields[0] is skipped: 'Software Engineering Co | Designer' must not
    score as a software engineering role."""
    f = ["Software Engineer Co", "Designer", "San Francisco"]
    assert hn.pick_title(f, " | ".join(f)) is None


def test_a_senior_only_post_is_not_a_title_match():
    f = ["Acme", "Senior Staff Backend Engineer", "San Francisco"]
    assert hn.pick_title(f, " | ".join(f)) is None


# --- location selection ---------------------------------------------------

def test_bay_area_field_wins_over_remote():
    f = ["Acme", "Backend Engineer", "REMOTE", "San Francisco, CA"]
    assert hn.pick_location(f, " | ".join(f)) == "San Francisco, CA"


def test_falls_back_to_remote_when_no_city_named():
    f = ["Acme", "Backend Engineer", "REMOTE (US)"]
    assert hn.pick_location(f, " | ".join(f)) == "REMOTE (US)"


def test_falls_back_to_a_city_mentioned_in_the_body():
    f = ["Acme", "Backend Engineer", "FULL-TIME"]
    text = " | ".join(f) + ". Our office is in Palo Alto and we meet weekly."
    assert hn.pick_location(f, text) == "Palo Alto"


def test_no_location_anywhere_returns_none():
    f = ["Acme", "Backend Engineer", "FULL-TIME"]
    assert hn.pick_location(f, " | ".join(f)) is None


# --- row construction: the filters are find_jobs.py's ---------------------

def test_a_bay_area_new_grad_post_becomes_a_row():
    text = ("Acme (YC S24) | Backend Engineer | San Francisco, CA | FULL-TIME | "
            "https://acme.example/jobs. We are hiring new grads, 1 year of "
            "experience is plenty. Mentorship from senior engineers.")
    row = hn.make_row(comment(text, author="founder", cid=42), THREAD)
    assert row is not None
    assert row["company"] == "Acme"
    assert row["title"] == "Backend Engineer"
    assert row["bay_area"] == "yes"
    assert row["source"] == "hn"
    assert row["years_stated"] == 1
    assert "new grad" in row["good_signals"]
    assert row["hn_author"] == "founder"          # the contact
    assert row["comment_url"] == "https://news.ycombinator.com/item?id=42"
    assert row["url"] == "https://acme.example/jobs"
    assert row["thread"] == "2026-10-01"


def test_non_bay_non_remote_post_is_dropped():
    text = "Acme | Backend Engineer | New York City, NY | FULL-TIME"
    assert hn.make_row(comment(text), THREAD) is None


def test_over_experienced_post_is_dropped():
    text = ("Acme | Backend Engineer | San Francisco | we need 5+ years of "
            "professional software engineering experience")
    assert hn.make_row(comment(text), THREAD) is None


def test_range_is_screened_on_the_ceiling():
    text = "Acme | Backend Engineer | San Francisco | 1-3 years of experience"
    assert hn.make_row(comment(text), THREAD) is None


def test_post_with_no_recognisable_role_is_dropped():
    """Precision over recall: guessing a title would mis-score the row."""
    text = ("Thunder Compute (YC S24) | C++ Systems, Infrastructure, BizOps | "
            "San Francisco (Onsite) | https://thundercompute.example")
    assert hn.make_row(comment(text), THREAD) is None


def test_prose_without_the_pipe_convention_is_dropped():
    text = "We are hiring a backend engineer in San Francisco, email me!"
    assert hn.make_row(comment(text), THREAD) is None


def test_empty_comment_is_dropped():
    assert hn.make_row(comment(""), THREAD) is None
    assert hn.make_row({"text": None, "author": "x", "id": 1}, THREAD) is None


def test_html_escaped_comment_text_is_decoded():
    """HN serves entity-escaped HTML; strip_html must unescape before
    stripping or the keyword matching reads literal markup."""
    text = ("Acme | Backend Engineer | San Francisco | we&#x27;re hiring "
            "<i>new grads</i> &amp; juniors")
    row = hn.make_row(comment(text), THREAD)
    assert row is not None
    assert "<" not in row["title"]
    assert "new grad" in row["good_signals"]


def test_all_locations_flag_keeps_a_non_bay_post():
    text = "Acme | Backend Engineer | Berlin, Germany | FULL-TIME"
    assert hn.make_row(comment(text), THREAD) is None
    row = hn.make_row(comment(text), THREAD, all_locations=True)
    assert row is not None and row["bay_area"] == ""


def test_crunch_language_is_recorded_and_penalised():
    base = "Acme | Backend Engineer | San Francisco | 1 year of experience"
    crunchy = base + ". We do whatever it takes and grind."
    clean = hn.make_row(comment(base), THREAD)
    flagged = hn.make_row(comment(crunchy), THREAD)
    assert flagged["crunch_flags"]
    assert flagged["fit_score"] < clean["fit_score"]


# --- scoring comes from find_jobs, not a second copy ----------------------

def test_scoring_matches_find_jobs_for_the_same_inputs():
    text = "Acme | Backend Engineer | San Francisco | 1 year of experience"
    row = hn.make_row(comment(text), THREAD)
    expected = fj.fit_score("Backend Engineer", "San Francisco",
                            fj.strip_html(text), (1, 1), [])
    assert row["fit_score"] == expected


def test_csv_fields_cover_every_row_key():
    text = "Acme | Backend Engineer | San Francisco | 1 year of experience"
    row = hn.make_row(comment(text), THREAD)
    assert set(row) == set(hn.CSV_FIELDS)


def test_hn_csv_keeps_the_contact_column_find_jobs_lacks():
    assert "hn_author" in hn.CSV_FIELDS
    assert "hn_author" not in fj.CSV_FIELDS
