"""Tests for the weekly runner.

The network-facing parts are driven with fake threads and a fake session, so
these run offline. What matters here is that discovery only ever appends slugs
that were verified live, and that the merged CSV stays clickable.
"""

import csv
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import find_jobs as fj      # noqa: E402
import hn_hiring as hn      # noqa: E402
import weekly               # noqa: E402

COMMENT = (
    'Acme | Backend Engineer | San Francisco '
    '<a href="https://jobs.ashbyhq.com/acme-labs">apply</a> '
    'Beta | Data Engineer | Palo Alto '
    '<a href="https://job-boards.greenhouse.io/betaco/jobs/123">apply</a> '
    'Gamma <a href="https://jobs.lever.co/gammainc/abc">apply</a> '
    'Delta <a href="https://ats.rippling.com/en-GB/deltaco/jobs/x">apply</a> '
    'Noise <a href="https://boards.greenhouse.io/embed/job_board?for=epsilon">x</a>'
)


class FakeSession:
    """Serves one thread of one comment; every board check reports 2 jobs."""

    def __init__(self, dead=()):
        self.dead = set(dead)

    def get(self, url, timeout=None, allow_redirects=False):
        return FakeResponse(url, self.dead)


class FakeResponse:
    def __init__(self, url, dead):
        self.url = url
        self._dead = dead
        self.status_code = 404 if any(d in url for d in dead) else 200

    def json(self):
        return {"jobs": [{"title": "Backend Engineer"}, {"title": "x"}]}

    def raise_for_status(self):
        pass


@pytest.fixture
def fake_hn(monkeypatch):
    monkeypatch.setattr(hn, "fetch_threads",
                        lambda s, m: [{"id": "1", "created": "2026-10-01"}])
    monkeypatch.setattr(hn, "fetch_comments",
                        lambda s, i: [{"text": COMMENT, "author": "a", "id": 9}])


# --- discovery ------------------------------------------------------------

def test_harvest_finds_board_slugs_on_every_platform(fake_hn):
    found, n = weekly.harvest_slugs(FakeSession(), 1)
    assert n == 1
    assert found["ashby"] == {"acme-labs"}
    assert "betaco" in found["greenhouse"]
    assert found["lever"] == {"gammainc"}
    assert found["rippling"] == {"deltaco"}


def test_harvest_reads_the_greenhouse_embed_form(fake_hn):
    found, _ = weekly.harvest_slugs(FakeSession(), 1)
    assert "epsilon" in found["greenhouse"]


def test_harvest_skips_platform_pages_not_companies(fake_hn):
    found, _ = weekly.harvest_slugs(FakeSession(), 1)
    for slugs in found.values():
        assert not (slugs & weekly.NOT_A_COMPANY)


def test_rippling_locale_prefix_is_not_mistaken_for_the_slug(fake_hn):
    """ats.rippling.com/en-GB/deltaco/... must yield deltaco, not en-gb."""
    found, _ = weekly.harvest_slugs(FakeSession(), 1)
    assert "en-gb" not in found["rippling"]


def test_slugs_from_text_reads_every_platform_pattern():
    blob = ("https://jobs.ashbyhq.com/acme "
            "https://job-boards.greenhouse.io/beta/jobs/1 "
            "https://boards.greenhouse.io/embed/job_board?for=gamma "
            "https://jobs.lever.co/delta/x "
            "https://ats.rippling.com/en-GB/epsilon/jobs/y")
    found = weekly.slugs_from_text(blob)
    assert found["ashby"] == {"acme"}
    assert found["greenhouse"] == {"beta", "gamma"}
    assert found["lever"] == {"delta"}
    assert found["rippling"] == {"epsilon"}


def test_slugs_from_text_ignores_platform_pages():
    found = weekly.slugs_from_text("https://jobs.ashbyhq.com/search")
    assert found["ashby"] == set()


def test_all_tracked_spans_every_file_and_the_exclude_list(tmp_path, monkeypatch):
    """Deduping per-platform would re-add a slug that is already live on a
    different one, which is how the existing (404) entries were created."""
    gh = tmp_path / "gh.txt"
    gh.write_text("sharedco\n")
    ash = tmp_path / "ash.txt"
    ash.write_text("other\n# oldco  (404)\n")
    monkeypatch.setattr(fj, "SLUG_FILES",
                        {"greenhouse": str(gh), "ashby": str(ash)})
    known = weekly.all_tracked()
    assert {"sharedco", "other", "oldco"} <= known
    assert "leverdemo-8" in known          # EXCLUDE_SLUGS folded in
    # the point: a slug live on greenhouse is not a new ashby company
    assert "sharedco" in known


def test_verify_keeps_live_boards_and_drops_dead_ones():
    live = weekly.verify_boards(FakeSession(dead=["gone"]),
                                {"ashby": ["alive", "gone"]}, workers=2)
    assert live["ashby"] == [("alive", 2)]


def test_verify_drops_a_board_with_zero_postings():
    class Empty(FakeSession):
        def get(self, url, timeout=None, allow_redirects=False):
            r = FakeResponse(url, set())
            r.json = lambda: {"jobs": []}
            return r

    live = weekly.verify_boards(Empty(), {"ashby": ["quiet"]}, workers=1)
    assert live["ashby"] == []


# --- writing slugs --------------------------------------------------------

def test_append_slugs_adds_under_a_dated_header(tmp_path, monkeypatch):
    path = tmp_path / "companies_ashby.txt"
    path.write_text("existing\n")
    monkeypatch.setattr(fj, "SLUG_FILES", {"ashby": str(path)})
    n = weekly.append_slugs({"ashby": [("newco", 3)]}, "2026-10-19")
    body = path.read_text()
    assert n == 1
    assert "# added 2026-10-19 from HN hiring threads" in body
    assert "existing" in body and "newco" in body
    assert fj.load_slugs(str(path)) == ["existing", "newco"]


def test_tracked_slugs_includes_pruned_dead_entries(tmp_path, monkeypatch):
    """A slug previously pruned as dead must not be rediscovered and re-added
    every single week."""
    path = tmp_path / "companies_ashby.txt"
    path.write_text("alive\n# deadco  (404)\n")
    monkeypatch.setattr(fj, "SLUG_FILES", {"ashby": str(path)})
    known = weekly.tracked_slugs()
    assert known["ashby"] == {"alive", "deadco"}


# --- merging --------------------------------------------------------------

def test_merge_ranks_both_sources_together(tmp_path):
    ats = tmp_path / "jobs_new.csv"
    with open(ats, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fj.CSV_FIELDS)
        w.writeheader()
        w.writerow({**dict.fromkeys(fj.CSV_FIELDS, ""), "fit_score": "50",
                    "company": "ats-co", "source": "ashby",
                    "title": "Backend Engineer", "url": "https://a.example"})
    hnf = tmp_path / "hn_new.csv"
    with open(hnf, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hn.CSV_FIELDS)
        w.writeheader()
        w.writerow({**dict.fromkeys(hn.CSV_FIELDS, ""), "fit_score": "70",
                    "company": "hn-co", "source": "hn",
                    "title": "Data Engineer", "hn_author": "someone",
                    "url": "https://b.example",
                    "comment_url": "https://news.ycombinator.com/item?id=1"})
    out = tmp_path / "weekly_new.csv"
    rows = weekly.merge_new([str(ats), str(hnf)], str(out))
    assert [r["fit_score"] for r in rows] == ["70", "50"]
    assert rows[0]["hn_author"] == "someone"
    assert set(rows[0]) == set(weekly.MERGED_FIELDS)


def test_merged_csv_quotes_fields_so_links_stay_clickable(tmp_path):
    hnf = tmp_path / "hn_new.csv"
    with open(hnf, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hn.CSV_FIELDS)
        w.writeheader()
        w.writerow({**dict.fromkeys(hn.CSV_FIELDS, ""), "fit_score": "40",
                    "company": "c", "source": "hn", "title": "Backend",
                    "url": "https://a.example/jobs",
                    "comment_url": "https://news.ycombinator.com/item?id=2"})
    out = tmp_path / "weekly_new.csv"
    weekly.merge_new([str(hnf)], str(out))
    raw = out.read_text()
    assert ",https://" not in raw, "a bare URL follows a comma"


def test_merge_tolerates_a_missing_source_file(tmp_path):
    out = tmp_path / "weekly_new.csv"
    assert weekly.merge_new([str(tmp_path / "nope.csv")], str(out)) == []
    assert out.exists()


# --- archiving ------------------------------------------------------------

def test_archive_copies_outputs_and_leaves_originals(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(weekly, "ARCHIVE_DIR", "jobs")
    (tmp_path / "jobs.csv").write_text("a,b\n1,2\n")
    saved = weekly.archive_outputs("2026-10-19")
    assert saved == [str(pathlib.Path("jobs") / "jobs_2026-10-19.csv")]
    assert (tmp_path / "jobs.csv").exists()
    assert (tmp_path / "jobs" / "jobs_2026-10-19.csv").read_text() == "a,b\n1,2\n"


def test_archive_does_not_clobber_an_existing_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(weekly, "ARCHIVE_DIR", "jobs")
    (tmp_path / "jobs.csv").write_text("new\n")
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "jobs_2026-10-19.csv").write_text("original\n")
    assert weekly.archive_outputs("2026-10-19") == []
    assert (tmp_path / "jobs" / "jobs_2026-10-19.csv").read_text() == "original\n"
