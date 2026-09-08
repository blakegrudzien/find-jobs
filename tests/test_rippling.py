"""Tests for the Rippling fetcher.

Rippling is the only source needing two requests per posting, which makes it
the only one that can fail halfway through a board. These use recorded
response shapes (captured from the live API in September 2026) rather than the
network, so they run offline and stay stable.
"""

import importlib.util
import pathlib

import pytest
import requests

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("find_jobs",
                                                  _ROOT / "find_jobs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fj = _load()

# --- recorded shapes ------------------------------------------------------
# Trimmed from real responses. The exact nesting is the point: description is
# a {"company", "role"} dict, and workLocations is a list of plain strings.

LIST_PAYLOAD = [
    {"uuid": "u-backend", "name": "Backend Engineer",
     "department": {"id": "Eng", "label": "Eng"},
     "url": "https://ats.rippling.com/acme/jobs/u-backend",
     "workLocation": {"label": "Pittsburgh, PA", "id": "Pittsburgh, PA"}},
    {"uuid": "u-senior", "name": "Senior Staff Engineer",
     "url": "https://ats.rippling.com/acme/jobs/u-senior",
     "workLocation": {"label": "San Francisco, CA", "id": "San Francisco, CA"}},
    {"uuid": "u-sales", "name": "Account Executive",
     "url": "https://ats.rippling.com/acme/jobs/u-sales",
     "workLocation": {"label": "Austin, TX", "id": "Austin, TX"}},
]

DETAIL_BACKEND = {
    "uuid": "u-backend", "name": "Backend Engineer",
    "workLocations": ["Pittsburgh, PA", "San Francisco, CA"],
    "description": {
        "company": "<p>We are a company. We hire new graduates.</p>",
        "role": "<p>You will build things. 1 year of experience.</p>",
    },
    "url": "https://ats.rippling.com/acme/jobs/u-backend",
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code}")
            err.response = self
            raise err


class FakeSession:
    """Serves the list once, then per-uuid details. Records every URL."""

    def __init__(self, details, detail_status=None, list_payload=None):
        self.details = details
        self.detail_status = detail_status or {}
        self.list_payload = LIST_PAYLOAD if list_payload is None else list_payload
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if url.endswith("/jobs"):
            return FakeResponse(self.list_payload)
        uuid = url.rsplit("/", 1)[-1]
        status = self.detail_status.get(uuid, 200)
        return FakeResponse(self.details.get(uuid, {}), status)


def test_only_title_matching_postings_are_fetched_in_detail():
    """The whole point of the two-phase design: a 682-job board must not
    become 682 detail requests."""
    s = FakeSession({"u-backend": DETAIL_BACKEND})
    fj.fetch_rippling("acme", s)
    detail_calls = [c for c in s.calls if not c.endswith("/jobs")]
    assert len(detail_calls) == 1
    assert detail_calls[0].endswith("u-backend")


def test_location_uses_the_full_detail_list_not_the_lossy_list_entry():
    """The list showed only Pittsburgh; the detail includes San Francisco.
    Pre-filtering on the list entry would have dropped this Bay Area role."""
    s = FakeSession({"u-backend": DETAIL_BACKEND})
    rows = fj.fetch_rippling("acme", s)
    assert len(rows) == 1
    assert rows[0]["bay_area"] == "yes"
    assert "San Francisco, CA" in rows[0]["location"]


def test_both_description_halves_are_searched():
    """Requirements live in "role"; culture language lives in "company".
    Dropping either half loses signal the filter and score depend on."""
    body = fj._rippling_body(DETAIL_BACKEND["description"])
    assert "new graduates" in body       # from "company"
    assert "1 year of experience" in body  # from "role"
    assert "<p>" not in body


def test_years_and_signals_are_read_from_the_assembled_body():
    s = FakeSession({"u-backend": DETAIL_BACKEND})
    row = fj.fetch_rippling("acme", s)[0]
    assert row["years_stated"] == 1
    assert "new graduate" in row["good_signals"]
    assert row["source"] == "rippling"
    assert row["url"] == "https://ats.rippling.com/acme/jobs/u-backend"


def test_board_size_counts_every_posting_not_just_matches():
    s = FakeSession({"u-backend": DETAIL_BACKEND})
    row = fj.fetch_rippling("acme", s)[0]
    assert row["board_size"] == len(LIST_PAYLOAD)


def test_a_posting_pulled_between_the_two_calls_is_skipped():
    s = FakeSession({}, detail_status={"u-backend": 404})
    assert fj.fetch_rippling("acme", s) == []


def test_a_failing_detail_fetch_fails_the_whole_board():
    """A silently-skipped posting is indistinguishable from "no matching
    roles" — the exact failure this project already fixed once in scrape_one.
    The board must be reported as failed so the seen-store is not updated."""
    s = FakeSession({}, detail_status={"u-backend": 500})
    with pytest.raises(requests.HTTPError):
        fj.fetch_rippling("acme", s)


def test_scrape_one_classifies_a_failed_rippling_board():
    s = FakeSession({}, detail_status={"u-backend": 500})
    source, slug, rows, status = fj.scrape_one("rippling", "acme", s)
    assert rows == []
    assert status.startswith("http500")


def test_body_falls_back_when_description_is_not_a_dict():
    assert fj._rippling_body("<p>plain</p>") == "plain"
    assert fj._rippling_body(None) == ""


def test_location_falls_back_to_the_list_entry_when_detail_has_none():
    detail = dict(DETAIL_BACKEND)
    detail.pop("workLocations")
    sf_listing = [dict(LIST_PAYLOAD[0],
                       workLocation={"label": "San Francisco, CA",
                                     "id": "San Francisco, CA"})]
    s = FakeSession({"u-backend": detail}, list_payload=sf_listing)
    rows = fj.fetch_rippling("acme", s)
    assert rows and rows[0]["location"] == "San Francisco, CA"


def test_a_non_bay_non_remote_location_is_still_filtered_out():
    """The fallback must not become a bypass: Pittsburgh-only still drops."""
    detail = dict(DETAIL_BACKEND)
    detail["workLocations"] = ["Pittsburgh, PA"]
    s = FakeSession({"u-backend": detail})
    assert fj.fetch_rippling("acme", s) == []


# --- wiring ---------------------------------------------------------------

def test_rippling_is_registered_everywhere_it_needs_to_be():
    assert "rippling" in fj.SLUG_FILES
    assert "rippling" in fj.FETCHERS
    assert fj.SLUG_FILES["rippling"] == "companies_rippling.txt"


def test_every_slug_file_has_a_fetcher_and_vice_versa():
    assert set(fj.SLUG_FILES) == set(fj.FETCHERS)


def test_the_shipped_slug_file_parses():
    slugs = fj.load_slugs(str(_ROOT / "companies_rippling.txt"))
    assert "rippling" in slugs
