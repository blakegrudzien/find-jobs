"""Tests for the scoring fixes made during the September 2026 review.

Each test here corresponds to a defect that was live before that pass. They
are separated from test_regressions.py only so the provenance stays legible:
those pin bugs that shipped and were fixed earlier, these pin bugs found by
reading the code against the goals in CLAUDE.md.
"""

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


# --- a range carries a floor and a ceiling, and they do different jobs -----

@pytest.mark.parametrize("body,floor,ceiling", [
    ("0-2 years of experience", 0, 2),
    ("1 year of experience", 1, 1),
    ("2+ years of experience", 2, 2),
    ("1-3 years of experience", 1, 3),
    ("No experience required.", 0, 0),
])
def test_years_required_returns_floor_and_ceiling(body, floor, ceiling):
    assert fj.years_required(body) == (floor, ceiling)


def test_filter_still_screens_on_the_ceiling():
    # the documented fix: "1-3 years" is a 3-year req and must be dropped
    assert fj.max_years_required("1-3 years of experience") == 3
    assert fj.make_row("c", "greenhouse", "Backend Engineer", "San Francisco",
                       "u", "1-3 years of experience") is None


def test_explicit_zero_to_two_scores_the_same_as_one_year():
    """The inversion this fix exists for.

    "0-2 years" means they are open to someone with none. Scoring it on the
    ceiling ranked it as a two-year req, 9 points below an explicit "1 year",
    which mis-ordered the only part of the list that gets read. A stated floor
    of 0 and a stated floor of 1 are the same signal and now score the same.
    """
    common = {"title": "Backend Engineer", "location": "San Francisco",
              "body": ""}
    zero_two = fj.fit_score(years=(0, 2), crunch=[], **common)
    one_year = fj.fit_score(years=(1, 1), crunch=[], **common)
    two_year = fj.fit_score(years=(2, 2), crunch=[], **common)
    assert zero_two == one_year, "a stated 0-N is as strong a signal as a 1"
    assert zero_two > two_year, "a 0-N req must not score as a hard N req"


def test_unstated_years_scores_below_an_explicit_low_floor():
    """An unstated requirement is usually fine but more ambiguous than a
    stated one, so it must NOT collect the same bonus as an explicit "0-2"."""
    common = {"title": "Backend Engineer", "location": "San Francisco",
              "body": ""}
    unstated = fj.fit_score(years=(0, 0), crunch=[], **common)
    zero_two = fj.fit_score(years=(0, 2), crunch=[], **common)
    assert unstated < zero_two
    assert zero_two - unstated == fj.W_YEARS_LOW - fj.W_YEARS_UNSTATED


def test_zero_to_two_matches_one_year_end_to_end():
    def score(body):
        return fj.make_row("c", "greenhouse", "Backend Engineer",
                           "San Francisco", "u", body)["fit_score"]

    # These bodies must actually match YEARS_PATTERN, which needs a qualifying
    # word after the number ("experience"). A bare "Requires 1 year." parses as
    # (0, 0) — unstated — and would make this pass without exercising the
    # years component at all.
    assert fj.years_required("0-2 years of experience") == (0, 2)
    assert fj.years_required("1 year of experience") == (1, 1)
    # "0-2 years" is itself an EXPLICIT_NEWGRAD phrase and "1 year" is not, so
    # the only difference left between the two is that one body bonus
    assert score("0-2 years of experience") == \
        score("1 year of experience") + fj.W_NEWGRAD_BODY_PER_HIT


def test_zero_to_two_beats_a_flat_two_end_to_end():
    def score(body):
        return fj.make_row("c", "greenhouse", "Backend Engineer",
                           "San Francisco", "u", body)["fit_score"]

    assert score("0-2 years of experience") > score("2+ years of experience")


# --- substring double-counting --------------------------------------------

def test_find_flags_drops_substring_shadowed_matches():
    assert fj.find_flags("we hire new graduates", ["new grad", "new graduate"]) \
        == ["new graduate"]


def test_new_graduate_scores_once_not_twice():
    hits = fj.find_flags("We hire new graduates every year.",
                         fj.EXPLICIT_NEWGRAD)
    assert hits == ["new graduate"], (
        "'new grad' is a substring of 'new graduate'; counting both scored one "
        "phrase for 12 of the 18-point cap")


def test_good_signals_does_not_waste_slots_on_mentor_and_mentorship():
    assert fj.find_flags("we offer mentorship", fj.GOOD_FLAGS) == ["mentorship"]


def test_distinct_phrases_still_each_count():
    hits = fj.find_flags("new grad role, entry level, rotational program",
                         fj.EXPLICIT_NEWGRAD)
    assert len(hits) == 3


@pytest.mark.parametrize("flags", ["EXPLICIT_NEWGRAD", "GOOD_FLAGS",
                                   "CRUNCH_FLAGS", "TITLE_NEWGRAD",
                                   "MENTOR_YOU", "MENTOR_OTHERS",
                                   "STARTUP_SIGNALS", "TOO_EARLY_SIGNALS"])
def test_no_flag_list_has_duplicate_entries(flags):
    values = getattr(fj, flags)
    assert len(values) == len(set(values)), f"{flags} has duplicates"


# --- crunch penalty is capped explicitly, not by a display slice -----------

def test_crunch_penalty_is_five_each_up_to_the_cap():
    base = fj.fit_score("Backend Engineer", "San Francisco", "", (1, 1), [])
    one = fj.fit_score("Backend Engineer", "San Francisco", "", (1, 1), ["a"])
    assert base - one == fj.CRUNCH_PENALTY_EACH


def test_crunch_penalty_stops_at_the_cap():
    base = fj.fit_score("Backend Engineer", "San Francisco", "", (1, 1), [])
    many = fj.fit_score("Backend Engineer", "San Francisco", "", (1, 1),
                        ["a", "b", "c", "d", "e"])
    assert base - many == fj.CRUNCH_PENALTY_EACH * fj.CRUNCH_PENALTY_MAX_FLAGS


def test_every_crunch_flag_is_recorded_even_past_the_cap():
    body = "whatever it takes, grind, sacrifice, lose sleep, 80 hours"
    row = fj.make_row("c", "greenhouse", "Backend Engineer", "San Francisco",
                      "u", body)
    assert len(row["crunch_flags"].split(", ")) == 5, (
        "the column is the culture screen; truncating it destroyed the "
        "evidence needed to audit whether the penalty cap was binding")


# --- mentorship signal must not fire on investor boilerplate --------------

@pytest.mark.parametrize("body", [
    "We are backed and supported by Sequoia and a16z.",
    "You will work with guidance documents in the wiki.",
])
def test_mentorship_bonus_does_not_fire_on_boilerplate(body):
    assert not any(k in body.lower() for k in fj.MENTOR_YOU)


@pytest.mark.parametrize("body", [
    "You'll be mentored by a senior engineer.",
    "We provide real mentorship and coaching.",
    "You will have a dedicated mentor for your first six months.",
])
def test_mentorship_bonus_still_fires_on_real_signals(body):
    assert any(k in body.lower() for k in fj.MENTOR_YOU)


# --- keyword list invariants ----------------------------------------------

def test_every_title_keyword_maps_to_a_priority_tier():
    """A keyword that passes the filter but matches no tier scores 0 for role
    type, sinking the posting to the bottom of a list read top-down — the
    failure would never be seen."""
    tiers = [p for tier, _ in fj.TITLE_PRIORITY for p in tier]
    untiered = [k for k in fj.TITLE_KEYWORDS
                if not any(p in k or k in p for p in tiers)]
    assert not untiered, f"no tier for: {untiered}"


def test_field_application_engineer_still_passes_and_scores_as_an_se():
    """FAE is the same job as a solutions engineer. The bare "field
    application" keyword was dropped, so this must still pass via
    "application engineer" and land in the 12-point tier."""
    row = fj.make_row("c", "greenhouse", "Field Application Engineer",
                      "San Francisco", "u", "")
    assert row is not None and row["fit_score"] > 0
    assert "field application" not in fj.TITLE_KEYWORDS


def test_field_application_non_engineering_titles_are_not_matched():
    assert fj.title_matches("Field Application Specialist") is False
    assert fj.title_matches("Field Agent") is False


@pytest.mark.parametrize("title", [
    "Field Applications Engineer",
    "Applications Engineer",
    "Technical Applications Engineer",
])
def test_plural_applications_engineer_matches(title):
    """The plural is the more common spelling of the title and contains
    neither "field application" nor "application engineer" — the "s" breaks
    the singular keyword — so it needs its own entry."""
    assert fj.title_matches(title) is True


@pytest.mark.parametrize("title", [
    "Data Platform Engineer",
    "Data Platform Specialist",
    "Data Infrastructure Engineer",
])
def test_data_platform_and_infrastructure_titles_pass(title):
    assert fj.title_matches(title) is True


def test_seniority_still_disqualifies_the_plural_form():
    assert fj.title_matches("Senior Applications Engineer") is False


# --- grad date is overridable and parsed strictly -------------------------

def test_parse_grad_date_accepts_a_well_formed_value():
    assert fj.parse_grad_date("2026-05") == (2026, 5)


@pytest.mark.parametrize("bad", ["2026", "2026-13", "may-2026", "", "26-05"])
def test_parse_grad_date_rejects_malformed_input(bad):
    with pytest.raises(SystemExit):
        fj.parse_grad_date(bad)


def test_cohort_penalty_resolves_grad_date_at_call_time():
    """A default argument binds once at import, so --grad-date would have been
    silently ignored had the default stayed `grad_date=GRAD_DATE`."""
    text = "graduating December 2026"
    assert fj.cohort_later_than_yours(text, (2026, 5)) is True
    assert fj.cohort_later_than_yours(text, (2027, 5)) is False


# --- atomic writes --------------------------------------------------------

def test_atomic_write_leaves_no_temp_file_behind_on_failure(tmp_path):
    target = tmp_path / "slugs.txt"
    target.write_text("original\n")

    def boom(f):
        f.write("partial")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        fj.atomic_write(str(target), boom)
    assert target.read_text() == "original\n", "target must be untouched"
    assert not (tmp_path / "slugs.txt.tmp").exists()


def test_seen_store_round_trips(tmp_path):
    p = str(tmp_path / "seen.json")
    fj.save_seen(p, {"http://a": "2026-09-07"})
    assert fj.load_seen(p) == {"http://a": "2026-09-07"}


def test_load_seen_survives_a_corrupt_store(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("{not json")
    assert fj.load_seen(str(p)) == {}


# --- dedup ----------------------------------------------------------------

def test_dedup_key_collapses_formatting_differences():
    assert fj.dedup_key("Acme", "Backend Engineer", "San Francisco, CA") == \
        fj.dedup_key("acme", "Backend  Engineer", "san francisco  ca")


def test_rank_and_dedup_keeps_the_higher_scoring_copy():
    rows = [
        {"fit_score": 40, "company": "a", "title": "Backend Engineer",
         "location": "SF"},
        {"fit_score": 70, "company": "a", "title": "Backend Engineer",
         "location": "SF"},
        {"fit_score": 50, "company": "a", "title": "Data Engineer",
         "location": "SF"},
    ]
    out, collapsed = fj.rank_and_dedup(rows)
    assert collapsed == 1
    assert [r["fit_score"] for r in out] == [70, 50]
