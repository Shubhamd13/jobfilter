"""Rippling ATS fetcher: one job, board URLs and --expand.

Offline, against real Lyte responses saved 2026-10. The saved board list is
trimmed to three jobs (five entries: one job is listed per location).
"""

from __future__ import annotations

import json

import httpx
import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.cli import expand_companies, run
from jobfilter.fetchers import FetcherRegistry, HttpClient, JobNotFound
from jobfilter.models import Label, Origin, Platform, Status
from jobfilter.storage.store import company_name
from jobfilter.url_parser import parse_url

from .conftest import FIXTURES

RP = FIXTURES / "rippling"
API = "https://api.rippling.com/platform/api/ats/v1/board/lyte/jobs"
JOB_ID = "6d781383-02f4-47e5-bcf8-6d398590d476"
JOB_URL = f"https://ats.rippling.com/lyte/jobs/{JOB_ID}"
BOARD_URL = "https://ats.rippling.com/lyte/jobs"
BOARD_ENTRIES = json.loads((RP / "board_lyte_jobs.json").read_text())
BOARD_IDS = list(dict.fromkeys(e["uuid"] for e in BOARD_ENTRIES))


class FakeRippling(httpx.MockTransport):
    def __init__(self, missing: frozenset[str] = frozenset()) -> None:
        self.requests: list[str] = []
        self._missing = missing
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == API:
            return httpx.Response(200, json=BOARD_ENTRIES)
        job_id = url.removeprefix(API + "/")
        path = RP / f"job_lyte_{job_id}.json"
        if url.startswith(API + "/") and job_id not in self._missing and path.exists():
            return httpx.Response(200, json=json.loads(path.read_text()))
        return httpx.Response(404, json={"message": "Not found"})


def registry_for(transport: FakeRippling) -> FetcherRegistry:
    return FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None))


@pytest.fixture(scope="module")
def clf() -> RuleClassifier:
    return RuleClassifier()


def test_a_job_url_becomes_a_posting(clf: RuleClassifier) -> None:
    transport = FakeRippling()
    posting = registry_for(transport).fetch(parse_url(JOB_URL + "?jobSite=LinkedIn"))
    assert posting.platform is Platform.RIPPLING
    assert posting.company_slug == "lyte"
    assert company_name(posting) == "Lyte AI"
    assert posting.job_id == JOB_ID
    assert posting.title == "Robotics Engineer (Algorithms & Software)"
    assert posting.location == "Sunnyvale, CA"
    # Both description parts, company blurb first, as on the page.
    assert posting.description_text.startswith("About Lyte")
    assert "<p" not in posting.description_text
    # The form's work-authorization question is not posting text.
    assert "legally authorized" not in posting.description_text
    assert clf.classify(posting).label is Label.OPEN
    assert transport.requests == [f"{API}/{JOB_ID}"]


def test_a_job_in_several_locations_lists_them_all() -> None:
    multi = next(i for i in BOARD_IDS if sum(e["uuid"] == i for e in BOARD_ENTRIES) > 1)
    posting = registry_for(FakeRippling()).fetch(parse_url(f"https://ats.rippling.com/lyte/jobs/{multi}"))
    assert posting.location == "Sunnyvale, CA; Taiwan"


def test_a_missing_job_is_job_not_found() -> None:
    url = "https://ats.rippling.com/lyte/jobs/00000000-0000-4000-8000-000000000000"
    with pytest.raises(JobNotFound, match="does not exist"):
        registry_for(FakeRippling()).fetch(parse_url(url))


def test_a_board_url_checks_each_job_once(clf: RuleClassifier) -> None:
    transport = FakeRippling()
    results = run([BOARD_URL], registry_for(transport), clf)
    # Five list entries, three jobs.
    assert len(BOARD_ENTRIES) == 5
    assert [r.job_id for r in results] == BOARD_IDS
    assert {r.origin for r in results} == {Origin.DISCOVERED}
    assert {r.status for r in results} == {Status.OK}
    assert sum(u.startswith(API + "/") for u in transport.requests) == 3


def test_expand_from_a_job_url_skips_the_requested_job(clf: RuleClassifier) -> None:
    transport = FakeRippling()
    registry = registry_for(transport)
    requested = run([JOB_URL], registry, clf)
    discovered = expand_companies(requested, registry, clf)
    assert [r.job_id for r in discovered] == [i for i in BOARD_IDS if i != JOB_ID]
    assert transport.requests.count(f"{API}/{JOB_ID}") == 1


def test_a_board_job_that_fails_is_reported_with_its_page(clf: RuleClassifier) -> None:
    gone = BOARD_IDS[1]
    results = run([BOARD_URL], registry_for(FakeRippling(missing=frozenset({gone}))), clf)
    [failed] = [r for r in results if r.status is not Status.OK]
    assert failed.job_id == gone
    assert failed.url == f"https://ats.rippling.com/lyte/jobs/{gone}"
    assert len(results) == 3


def test_an_unknown_board_is_one_failed_row() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    registry = FetcherRegistry(HttpClient(transport=httpx.MockTransport(handler), backoff=0.0, sleep=lambda _: None))
    [result] = run(["https://ats.rippling.com/no-such-co/jobs"], registry, RuleClassifier())
    assert result.status is Status.FETCH_ERROR
    assert result.error_type == "BoardNotFound"


@pytest.mark.live
def test_live_job_matches_fixture() -> None:
    """Catches API drift, or the job closing: then re-save the fixtures."""
    with FetcherRegistry() as live:
        posting = live.fetch(parse_url(JOB_URL))
    assert posting.title == "Robotics Engineer (Algorithms & Software)"
    assert posting.description_text
