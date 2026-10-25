"""Board refresh: re-read stored companies and record what changed.

Offline. The Lever board is the saved one, served from a transport whose
answer the tests edit between refreshes; Rippling, Workday and Dover reuse
the fakes from their own test modules.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.cli import classify_posting, main
from jobfilter.fetchers import FetcherRegistry, HttpClient
from jobfilter.models import Change, CompanyRefresh, Origin, Platform, Posting
from jobfilter.refresh import refresh_boards, stored_boards
from jobfilter.report import render_refresh
from jobfilter.storage import Store
from jobfilter.url_parser import parse_url

from .conftest import LEVER_BOARD_SIZE, LEVER_SLUG, load_fixture
from .test_dover import JOB_URL as DOVER_JOB_URL
from .test_dover import FakeDover
from .test_rippling import API as RIPPLING_API
from .test_rippling import BOARD_IDS as RIPPLING_IDS
from .test_rippling import FakeRippling
from .test_storage import make_posting, make_result
from .test_workday import BD_API, BD_TRADE_IDS, BD_TRADE_JOB, FakeWorkday
from .test_workday import load as load_workday

LEVER_BOARD_URL = f"https://api.lever.co/v0/postings/{LEVER_SLUG}?mode=json"


class Boards(httpx.MockTransport):
    """Serves `routes` (API URL -> JSON), which a test may edit between refreshes."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[str] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url not in self.routes:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=self.routes[url])


def registry_for(transport: httpx.BaseTransport) -> FetcherRegistry:
    return FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None))


@pytest.fixture(scope="module")
def clf() -> RuleClassifier:
    return RuleClassifier()


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as store:
        yield store


@pytest.fixture
def lever() -> Boards:
    return Boards({LEVER_BOARD_URL: copy.deepcopy(load_fixture("lever_board_rai.json"))})


def keep(store: Store, clf: RuleClassifier, postings: list[Posting]) -> None:
    """Store postings as an earlier run would have."""
    store.record_run(
        "seed", [classify_posting(p, p.url, clf, Origin.DISCOVERED) for p in postings]
    )


def seed_board(store: Store, clf: RuleClassifier, transport: Boards) -> None:
    """Store the board exactly as the transport serves it now."""
    keep(store, clf, list(registry_for(transport).fetch_board(Platform.LEVER, LEVER_SLUG)))


def refresh(
    store: Store, clf: RuleClassifier, transport: httpx.BaseTransport, deep: bool = False
) -> CompanyRefresh:
    """Refresh every stored board with a fresh registry (per-run caches); the one outcome."""
    registry = registry_for(transport)
    outcomes = refresh_boards(store, registry, clf, stored_boards(store, registry), deep=deep)
    assert len(outcomes) == 1
    return outcomes[0]


def jobs(store: Store) -> dict[str, Any]:
    return store.company_jobs(store.connection.execute("SELECT id FROM companies").fetchone()[0])


# --------------------------------------------------------------------------
# What counts as a change (a whole-board platform: Lever)
# --------------------------------------------------------------------------

def test_an_unchanged_board_reports_nothing(store: Store, clf: RuleClassifier, lever: Boards) -> None:
    seed_board(store, clf, lever)
    outcome = refresh(store, clf, lever)

    assert outcome.ok and outcome.listed == LEVER_BOARD_SIZE
    assert outcome.changes == {} and outcome.changed == 0 and outcome.failed == 0
    assert store.count_postings(changed=True) == 0
    # It still counts as refreshed: "nothing changed" is not "never looked".
    assert store.company_summary()[0]["refresh"]["status"] == "ok"


def test_a_job_the_board_gained_is_new(store: Store, clf: RuleClassifier, lever: Boards) -> None:
    full = lever.routes[LEVER_BOARD_URL]
    lever.routes[LEVER_BOARD_URL] = full[1:]
    seed_board(store, clf, lever)
    lever.routes[LEVER_BOARD_URL] = full

    outcome = refresh(store, clf, lever)
    assert outcome.changes == {Change.NEW: 1}
    assert len(jobs(store)) == LEVER_BOARD_SIZE
    [flagged] = store.postings(changed=True)
    assert flagged["job_id"] == full[0]["id"] and flagged["changes"] == "new"
    assert flagged["origin"] == "discovered"


def test_an_edited_description_is_edited_not_relabeled(
    store: Store, clf: RuleClassifier, lever: Boards
) -> None:
    seed_board(store, clf, lever)
    lever.routes[LEVER_BOARD_URL][0]["descriptionPlain"] += "\nWe have a new office."

    outcome = refresh(store, clf, lever)
    assert outcome.changes == {Change.EDITED: 1}


def test_an_edit_that_flips_the_verdict_is_both(
    store: Store, clf: RuleClassifier, lever: Boards
) -> None:
    seed_board(store, clf, lever)
    job = lever.routes[LEVER_BOARD_URL][0]
    before = jobs(store)[job["id"]].label
    job["descriptionPlain"] += "\nMust be a U.S. citizen."

    outcome = refresh(store, clf, lever)
    assert outcome.changes == {Change.EDITED: 1, Change.RELABELED: 1}
    assert jobs(store)[job["id"]].label == "RESTRICTED_CITIZEN"
    detail = store.connection.execute(
        "SELECT detail FROM posting_changes WHERE kind = 'relabeled'"
    ).fetchone()[0]
    assert detail == f"{before} -> RESTRICTED_CITIZEN"


def test_a_job_the_board_dropped_is_closed_then_reopened(
    store: Store, clf: RuleClassifier, lever: Boards
) -> None:
    seed_board(store, clf, lever)
    full = lever.routes[LEVER_BOARD_URL]
    dropped = full[0]["id"]

    lever.routes[LEVER_BOARD_URL] = full[1:]
    outcome = refresh(store, clf, lever)
    assert outcome.changes == {Change.CLOSED: 1}
    assert outcome.listed == LEVER_BOARD_SIZE - 1
    assert jobs(store)[dropped].closed_at is not None
    assert len(jobs(store)) == LEVER_BOARD_SIZE  # kept, not deleted

    # Still gone: it is not closed a second time.
    assert refresh(store, clf, lever).changes == {}
    assert jobs(store)[dropped].closed_at is not None

    lever.routes[LEVER_BOARD_URL] = full
    assert refresh(store, clf, lever).changes == {Change.REOPENED: 1}
    assert jobs(store)[dropped].closed_at is None


def test_a_board_that_cannot_be_read_closes_nothing(
    store: Store, clf: RuleClassifier, lever: Boards
) -> None:
    seed_board(store, clf, lever)
    del lever.routes[LEVER_BOARD_URL]

    outcome = refresh(store, clf, lever)
    assert not outcome.ok and "no Lever job board" in (outcome.error or "")
    assert outcome.changes == {}
    assert all(job.closed_at is None for job in jobs(store).values())
    # Reported, not swallowed: a failure row and a failed refresh on the company.
    assert store.failures()[0]["error_type"] == "BoardNotFound"
    assert store.company_summary()[0]["refresh"]["status"] == "error"


def test_linkedin_and_indeed_companies_are_not_refreshed(
    store: Store, clf: RuleClassifier, lever: Boards
) -> None:
    seed_board(store, clf, lever)
    store.record_run("t", [
        make_result(make_posting(slug="seen", job_id="1", platform=Platform.LINKEDIN)),
        make_result(make_posting(slug="seen", job_id="2", platform=Platform.INDEED)),
        make_result(make_posting(slug="page", job_id="3", platform=Platform.WEB)),
    ])
    registry = registry_for(lever)
    boards = stored_boards(store, registry)
    assert [(b.platform, b.slug) for b in boards] == [(Platform.LEVER, LEVER_SLUG)]

    lever.requests.clear()
    refresh_boards(store, registry, clf, boards)
    assert lever.requests == [LEVER_BOARD_URL]


def test_a_refresh_is_one_recorded_run_with_progress(
    store: Store, clf: RuleClassifier, lever: Boards
) -> None:
    seed_board(store, clf, lever)
    runs_before = store.stats()["runs"]
    seen: list[tuple[int, int, str | None]] = []
    registry = registry_for(lever)

    refresh_boards(
        store, registry, clf, stored_boards(store, registry),
        progress=lambda done, total, current: seen.append(
            (len(done), total, current.slug if current else None)
        ),
    )
    assert seen == [(0, 1, LEVER_SLUG), (1, 1, None)]
    assert store.stats()["runs"] == runs_before + 1
    source = store.connection.execute("SELECT source FROM runs ORDER BY id DESC").fetchone()[0]
    assert source == "refresh"


# --------------------------------------------------------------------------
# One request per job: quick lists, deep re-reads (Rippling)
# --------------------------------------------------------------------------

def rippling_job(transport: FakeRippling, job_id: str) -> Posting:
    return registry_for(transport).fetch(
        parse_url(f"https://ats.rippling.com/lyte/jobs/{job_id}")
    )


def test_a_quick_refresh_fetches_only_jobs_it_does_not_hold(
    store: Store, clf: RuleClassifier
) -> None:
    keep(store, clf, [rippling_job(FakeRippling(), job_id) for job_id in RIPPLING_IDS[:2]])

    transport = FakeRippling()
    outcome = refresh(store, clf, transport)
    assert outcome.changes == {Change.NEW: 1} and outcome.listed == 3
    assert transport.requests == [RIPPLING_API, f"{RIPPLING_API}/{RIPPLING_IDS[2]}"]

    # Nothing new next time: the list alone.
    transport = FakeRippling()
    assert refresh(store, clf, transport).changes == {}
    assert transport.requests == [RIPPLING_API]


def test_a_deep_refresh_reads_every_job(store: Store, clf: RuleClassifier) -> None:
    keep(store, clf, [rippling_job(FakeRippling(), job_id) for job_id in RIPPLING_IDS])

    transport = FakeRippling()
    outcome = refresh(store, clf, transport, deep=True)
    assert outcome.changes == {} and outcome.listed == 3
    assert sorted(transport.requests) == sorted(
        [RIPPLING_API, *(f"{RIPPLING_API}/{job_id}" for job_id in RIPPLING_IDS)]
    )


def test_a_quick_refresh_closes_what_the_list_no_longer_has(
    store: Store, clf: RuleClassifier
) -> None:
    gone = make_posting(slug="lyte", job_id="no-longer-listed", platform=Platform.RIPPLING)
    keep(store, clf, [gone, *(rippling_job(FakeRippling(), j) for j in RIPPLING_IDS)])

    transport = FakeRippling()
    outcome = refresh(store, clf, transport)
    assert outcome.changes == {Change.CLOSED: 1}
    assert transport.requests == [RIPPLING_API]  # closing needs no job request
    assert jobs(store)["no-longer-listed"].closed_at is not None


@pytest.mark.parametrize("deep", [False, True], ids=["quick", "deep"])
def test_a_listed_job_that_will_not_fetch_is_failed_not_closed(
    store: Store, clf: RuleClassifier, deep: bool
) -> None:
    keep(store, clf, [rippling_job(FakeRippling(), job_id) for job_id in RIPPLING_IDS[:2]])

    outcome = refresh(store, clf, FakeRippling(missing=frozenset({RIPPLING_IDS[2]})), deep=deep)
    assert outcome.ok and outcome.failed == 1 and outcome.listed == 3
    assert outcome.changes == {}
    assert all(job.closed_at is None for job in jobs(store).values())
    failure = store.failures()[0]
    assert failure["url_as_given"].endswith(RIPPLING_IDS[2])
    assert failure["error_type"] == "JobNotFound"


# --------------------------------------------------------------------------
# Boards found from a stored job, not from the slug (Workday, Dover)
# --------------------------------------------------------------------------

def bd_whole_board(url: str, body: dict[str, Any]) -> httpx.Response:
    """Boston Dynamics' board with no search: the five saved jobs."""
    if url != f"{BD_API}/jobs" or body["searchText"] or body["appliedFacets"]:
        return httpx.Response(404)
    saved = load_workday("bostondynamics_search_global-trade.json")
    return httpx.Response(200, json=saved if not body["offset"] else {**saved, "jobPostings": []})


def test_workday_finds_its_site_from_a_stored_job(store: Store, clf: RuleClassifier) -> None:
    keep(store, clf, [registry_for(FakeWorkday()).fetch(parse_url(BD_TRADE_JOB))])

    transport = FakeWorkday(search=bd_whole_board)
    outcome = refresh(store, clf, transport)
    assert outcome.ok and outcome.listed == len(BD_TRADE_IDS)
    # The stored job is recognised in the listing by its id: neither fetched
    # again nor closed, and the other four arrive under the ids the list gave.
    assert outcome.changes == {Change.NEW: len(BD_TRADE_IDS) - 1}
    assert transport.count("GET") == len(BD_TRADE_IDS) - 1
    assert sorted(jobs(store)) == sorted(BD_TRADE_IDS)

    transport = FakeWorkday(search=bd_whole_board)
    assert refresh(store, clf, transport).changes == {}
    assert transport.count("GET") == 0


def test_workday_deep_refresh_reads_every_job(store: Store, clf: RuleClassifier) -> None:
    keep(store, clf, [registry_for(FakeWorkday()).fetch(parse_url(BD_TRADE_JOB))])

    transport = FakeWorkday(search=bd_whole_board)
    outcome = refresh(store, clf, transport, deep=True)
    assert outcome.changes == {Change.NEW: len(BD_TRADE_IDS) - 1}
    assert transport.count("GET") == len(BD_TRADE_IDS)


def test_dover_learns_its_client_from_a_stored_job(store: Store, clf: RuleClassifier) -> None:
    keep(store, clf, [registry_for(FakeDover()).fetch(parse_url(DOVER_JOB_URL))])

    transport = FakeDover()
    outcome = refresh(store, clf, transport)
    assert outcome.ok and outcome.listed == 9
    assert outcome.changes == {Change.NEW: 8}
    # One request to learn the client, one for the careers page, eight new jobs.
    assert len(transport.requests) == 10

    transport = FakeDover()
    assert refresh(store, clf, transport).changes == {}
    assert len(transport.requests) == 2


def test_dover_with_every_stored_job_gone_is_an_error_not_a_closure(
    store: Store, clf: RuleClassifier
) -> None:
    posting = registry_for(FakeDover()).fetch(parse_url(DOVER_JOB_URL))
    keep(store, clf, [posting])

    outcome = refresh(store, clf, FakeDover(missing=frozenset({posting.job_id})))
    assert not outcome.ok
    assert jobs(store)[posting.job_id].closed_at is None


# --------------------------------------------------------------------------
# CLI and report
# --------------------------------------------------------------------------

def test_render_refresh_puts_changed_companies_first() -> None:
    table = render_refresh(
        [
            CompanyRefresh(Platform.ASHBY, "quiet", None, "ok", listed=4),
            CompanyRefresh(Platform.LEVER, "broken", None, "error", error="no Lever job board"),
            CompanyRefresh(
                Platform.GREENHOUSE, "busy", "Busy Robots", "ok", listed=9,
                changes={Change.NEW: 2, Change.CLOSED: 1},
            ),
        ],
        width=120,
    )
    lines = table.splitlines()
    assert [line.split()[0] for line in lines[2:5]] == ["Busy", "broken", "quiet"]
    assert "no Lever job board" in lines[3]
    assert lines[-1] == "3 board(s) refreshed: 1 changed, 1 could not be read."


def test_render_refresh_with_nothing_stored() -> None:
    assert "no stored companies" in render_refresh([])


def test_cli_refresh(tmp_path, monkeypatch, capsys, clf: RuleClassifier, lever: Boards) -> None:
    db = tmp_path / "jobs.db"
    full = lever.routes[LEVER_BOARD_URL]
    lever.routes[LEVER_BOARD_URL] = full[1:]
    with Store(db) as store:
        seed_board(store, clf, lever)
    lever.routes[LEVER_BOARD_URL] = full
    monkeypatch.setattr(
        "jobfilter.cli.HttpClient",
        lambda timeout: HttpClient(transport=lever, backoff=0.0, sleep=lambda _: None),
    )

    assert main(["refresh", "--db", str(db), "--width", "120"]) == 0
    out = capsys.readouterr().out
    assert LEVER_SLUG in out and "1 board(s) refreshed: 1 changed." in out

    assert main(["refresh", "--db", str(db), "--company", "no-such-company"]) == 2
    assert "no stored company with a board: no-such-company" in capsys.readouterr().err

    del lever.routes[LEVER_BOARD_URL]
    assert main(["refresh", "--db", str(db), "--company", LEVER_SLUG]) == 1


def test_cli_refresh_of_an_empty_database(tmp_path, capsys) -> None:
    assert main(["refresh", "--db", str(tmp_path / "empty.db")]) == 0
    assert "no stored companies" in capsys.readouterr().out


@pytest.mark.live
def test_live_refresh_of_a_board_just_stored_finds_nothing(clf: RuleClassifier) -> None:
    with Store(":memory:") as store, FetcherRegistry() as registry:
        keep(store, clf, list(registry.fetch_board(Platform.LEVER, LEVER_SLUG)))
        with FetcherRegistry() as again:
            [outcome] = refresh_boards(store, again, clf, stored_boards(store, again))
        assert outcome.ok and outcome.listed > 0
        assert outcome.changes == {}
