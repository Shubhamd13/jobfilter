"""Whole-board fetching and --expand. Offline against the saved fixtures."""

from __future__ import annotations

from dataclasses import replace

import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.cli import _companies_in, classify_url, expand_companies, run
from jobfilter.fetchers import BoardFetcher, BoardNotFound, FetcherRegistry
from jobfilter.models import Origin, Platform, Status
from jobfilter.url_parser import parse_url

from .conftest import (
    ASHBY_BOARD_SIZE,
    ASHBY_JOB_ID,
    ASHBY_SLUG,
    ASHBY_URL,
    GH_BOARD_SIZE,
    GH_SLUG,
    GH_URL,
    LEVER_BOARD_SIZE,
    LEVER_SLUG,
    LEVER_URL,
    RecordingTransport,
)

BAD_URL = "https://www.linkedin.com/jobs/view/1"


@pytest.fixture(scope="module")
def clf() -> RuleClassifier:
    return RuleClassifier()


# --------------------------------------------------------------------------
# The capability itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "platform", [Platform.ASHBY, Platform.GREENHOUSE, Platform.LEVER]
)
def test_every_phase_one_platform_can_list_a_board(
    registry: FetcherRegistry, platform: Platform
) -> None:
    assert registry.supports_boards(platform)
    assert isinstance(registry.fetcher_for(platform), BoardFetcher)


@pytest.mark.parametrize(
    "platform,slug,size",
    [
        pytest.param(Platform.ASHBY, ASHBY_SLUG, ASHBY_BOARD_SIZE, id="ashby"),
        pytest.param(Platform.GREENHOUSE, GH_SLUG, GH_BOARD_SIZE, id="greenhouse"),
        pytest.param(Platform.LEVER, LEVER_SLUG, LEVER_BOARD_SIZE, id="lever"),
    ],
)
def test_fetch_board_returns_every_job_with_its_text(
    registry: FetcherRegistry, platform: Platform, slug: str, size: int
) -> None:
    postings = registry.fetch_board(platform, slug)

    assert len(postings) == size
    assert len({p.job_id for p in postings}) == size, "job ids are unique"
    for posting in postings:
        assert posting.platform is platform
        assert posting.company_slug == slug
        assert posting.title, f"{posting.job_id} has no title"
        assert posting.description_text, f"{posting.job_id} has no description"
        assert posting.url.startswith("https://"), "discovered jobs need a URL"


def test_a_discovered_job_matches_the_directly_fetched_one(
    registry: FetcherRegistry,
) -> None:
    """The board path and the single-job path must agree."""
    direct = registry.fetch(parse_url(ASHBY_URL))
    from_board = next(
        p
        for p in registry.fetch_board(Platform.ASHBY, ASHBY_SLUG)
        if p.job_id == ASHBY_JOB_ID
    )
    assert from_board.title == direct.title
    assert from_board.location == direct.location
    assert from_board.description_text == direct.description_text


def test_ashby_board_expansion_costs_no_extra_request(
    registry: FetcherRegistry, transport: RecordingTransport
) -> None:
    """fetch() already pulled the whole board to find one job."""
    registry.fetch(parse_url(ASHBY_URL))
    assert len(transport.requests) == 1

    postings = registry.fetch_board(Platform.ASHBY, ASHBY_SLUG)
    assert len(postings) == ASHBY_BOARD_SIZE
    assert len(transport.requests) == 1, "the cached board was reused"


@pytest.mark.parametrize(
    "platform,slug",
    [
        pytest.param(Platform.GREENHOUSE, GH_SLUG, id="greenhouse"),
        pytest.param(Platform.LEVER, LEVER_SLUG, id="lever"),
    ],
)
def test_other_boards_are_cached_per_slug(
    registry: FetcherRegistry,
    transport: RecordingTransport,
    platform: Platform,
    slug: str,
) -> None:
    registry.fetch_board(platform, slug)
    registry.fetch_board(platform, slug)
    assert len(transport.requests) == 1


def test_greenhouse_asks_for_content(
    registry: FetcherRegistry, transport: RecordingTransport
) -> None:
    """Without content=true the payload is titles only."""
    registry.fetch_board(Platform.GREENHOUSE, GH_SLUG)
    assert transport.requests[0].endswith("?content=true")


@pytest.mark.parametrize(
    "platform", [Platform.ASHBY, Platform.GREENHOUSE, Platform.LEVER]
)
def test_an_unknown_board_raises(
    registry: FetcherRegistry, platform: Platform
) -> None:
    with pytest.raises(BoardNotFound):
        registry.fetch_board(platform, "no-such-company")


# --------------------------------------------------------------------------
# The expansion pass
# --------------------------------------------------------------------------

def test_expansion_finds_the_rest_of_the_company(
    registry: FetcherRegistry, clf: RuleClassifier
) -> None:
    requested = run([ASHBY_URL], registry, clf)
    discovered = expand_companies(requested, registry, clf)

    assert len(discovered) == ASHBY_BOARD_SIZE - 1, "the requested job is excluded"
    assert all(r.origin is Origin.DISCOVERED for r in discovered)
    assert all(r.status is Status.OK for r in discovered)


def test_expansion_never_duplicates_a_requested_job(
    registry: FetcherRegistry, clf: RuleClassifier
) -> None:
    requested = run([ASHBY_URL, GH_URL, LEVER_URL], registry, clf)
    everything = requested + expand_companies(requested, registry, clf)

    keys = [(r.platform, r.company_slug, r.job_id) for r in everything]
    assert len(keys) == len(set(keys)), "a job appears at most once"
    assert len(everything) == ASHBY_BOARD_SIZE + GH_BOARD_SIZE + LEVER_BOARD_SIZE


def test_expansion_covers_all_three_platforms_at_once(
    registry: FetcherRegistry, clf: RuleClassifier
) -> None:
    requested = run([ASHBY_URL, GH_URL, LEVER_URL], registry, clf)
    discovered = expand_companies(requested, registry, clf)
    assert {r.platform for r in discovered} == {
        Platform.ASHBY,
        Platform.GREENHOUSE,
        Platform.LEVER,
    }


def test_expanding_twice_over_one_company_fetches_one_board(
    registry: FetcherRegistry, transport: RecordingTransport, clf: RuleClassifier
) -> None:
    sibling = f"https://jobs.ashbyhq.com/{ASHBY_SLUG}/x"
    requested = run([ASHBY_URL, sibling], registry, clf)
    transport.requests.clear()

    expand_companies(requested, registry, clf)
    assert transport.requests == [], "one company, one cached board"


def test_expansion_skips_urls_that_never_resolved(
    registry: FetcherRegistry, clf: RuleClassifier
) -> None:
    requested = run([BAD_URL], registry, clf)
    assert expand_companies(requested, registry, clf) == []


def test_expansion_of_a_dead_board_is_reported_not_swallowed(
    registry: FetcherRegistry, clf: RuleClassifier
) -> None:
    """A company whose board 404s still produces a row."""
    good = classify_url(ASHBY_URL, registry, clf)
    broken = replace(good, company_slug="no-such-company")
    discovered = expand_companies([broken], registry, clf)

    assert len(discovered) == 1
    assert discovered[0].status is Status.FETCH_ERROR
    assert discovered[0].origin is Origin.DISCOVERED
    assert discovered[0].error_type == "BoardNotFound"


def test_companies_in_is_deduplicated_and_ordered(
    registry: FetcherRegistry, clf: RuleClassifier
) -> None:
    results = run([GH_URL, ASHBY_URL, GH_URL, BAD_URL], registry, clf)
    assert _companies_in(results, registry) == [
        (Platform.GREENHOUSE, GH_SLUG),
        (Platform.ASHBY, ASHBY_SLUG),
    ]


def test_discovered_jobs_are_classified(
    registry: FetcherRegistry, clf: RuleClassifier
) -> None:
    """GrayMatter's board is mostly export-controlled; this is ground truth."""
    requested = run([ASHBY_URL], registry, clf)
    everything = requested + expand_companies(requested, registry, clf)

    restricted = [r for r in everything if r.label.is_restriction]
    assert len(restricted) > 1, "the requested job is not the only restricted one"
    assert all(r.evidence for r in restricted)


# --------------------------------------------------------------------------
# Live: the board endpoints are real and still shaped as the fixtures say
# --------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.parametrize(
    "platform,slug",
    [
        pytest.param(Platform.ASHBY, ASHBY_SLUG, id="ashby"),
        pytest.param(Platform.GREENHOUSE, GH_SLUG, id="greenhouse"),
        pytest.param(Platform.LEVER, LEVER_SLUG, id="lever"),
    ],
)
def test_live_boards_return_jobs_with_descriptions(
    platform: Platform, slug: str
) -> None:
    """If this fails, re-save the board fixtures."""
    with FetcherRegistry() as live:
        postings = live.fetch_board(platform, slug)
    assert postings
    assert all(p.title and p.description_text for p in postings)
