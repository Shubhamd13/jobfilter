"""Dover fetcher: one job, and --expand over the company's careers page.

Offline, against real Alloy Robotics responses saved 2026-10.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.cli import expand_companies, run
from jobfilter.fetchers import BoardNotFound, FetcherRegistry, HttpClient, JobNotFound
from jobfilter.models import Label, Origin, Platform, Status
from jobfilter.url_parser import parse_url

from .conftest import FIXTURES

DV = FIXTURES / "dover"
CLIENT_ID = "f68b464a-4241-42fb-aecc-c9e6364b9bc9"
JOB_ID = "8c782ebb-49ae-4bc9-9491-924ba4d53ac7"
JOB_URL = f"https://app.dover.com/apply/Alloy%20Robotics/{JOB_ID}/?rs=76643084"
BOARD_IDS = [j["id"] for j in json.loads((DV / f"careers-page_{CLIENT_ID}_jobs.json").read_text())["results"]]


class FakeDover(httpx.MockTransport):
    def __init__(self, missing: frozenset[str] = frozenset()) -> None:
        self.requests: list[str] = []
        self._missing = missing
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        name = None
        if url.startswith("https://app.dover.com/api/v1/inbound/application-portal-job/"):
            job_id = url.rsplit("/", 1)[1]
            name = None if job_id in self._missing else f"job_{job_id}.json"
        elif url == f"https://app.dover.com/api/v1/careers-page/{CLIENT_ID}/jobs":
            name = f"careers-page_{CLIENT_ID}_jobs.json"
        if name and (DV / name).exists():
            return httpx.Response(200, json=json.loads((DV / name).read_text()))
        return httpx.Response(404, json={"detail": "Not found."})


def registry_for(transport: FakeDover) -> FetcherRegistry:
    return FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None))


@pytest.fixture(scope="module")
def clf() -> RuleClassifier:
    return RuleClassifier()


def test_a_job_url_becomes_a_posting(clf: RuleClassifier) -> None:
    transport = FakeDover()
    posting = registry_for(transport).fetch(parse_url(JOB_URL))
    assert posting.platform is Platform.DOVER
    assert posting.company_slug == "alloy-robotics"
    assert posting.job_id == JOB_ID
    assert posting.title == "Forward-Deployed Engineer"
    assert posting.location == "San Francisco, CA"
    assert posting.url == JOB_URL, "keep the URL the user gave us"
    assert posting.description_text.startswith("About Alloy Robotics")
    assert "<p>" not in posting.description_text
    assert posting.raw["visa_support"] is False  # kept, not classified
    assert clf.classify(posting).label is Label.OPEN
    assert transport.requests == [
        f"https://app.dover.com/api/v1/inbound/application-portal-job/{JOB_ID}"
    ]


def test_a_missing_job_is_job_not_found() -> None:
    url = "https://app.dover.com/apply/Alloy%20Robotics/00000000-0000-4000-8000-000000000000"
    with pytest.raises(JobNotFound, match="taken down"):
        registry_for(FakeDover()).fetch(parse_url(url))


def test_expand_lists_the_company_from_the_job_fetched(clf: RuleClassifier) -> None:
    transport = FakeDover()
    registry = registry_for(transport)
    requested = run([JOB_URL], registry, clf)
    discovered = expand_companies(requested, registry, clf)

    assert [r.job_id for r in discovered] == [i for i in BOARD_IDS if i != JOB_ID]
    assert {r.origin for r in discovered} == {Origin.DISCOVERED}
    assert {r.company_slug for r in requested + discovered} == {"alloy-robotics"}
    # The requested job is not fetched twice.
    job_calls = [u for u in transport.requests if JOB_ID in u]
    assert len(job_calls) == 1


def test_a_board_job_that_fails_is_reported_with_its_page(clf: RuleClassifier) -> None:
    gone = BOARD_IDS[0]
    registry = registry_for(FakeDover(missing=frozenset({gone})))
    requested = run([JOB_URL], registry, clf)
    discovered = expand_companies(requested, registry, clf)
    [failed] = [r for r in discovered if r.status is not Status.OK]
    assert failed.job_id == gone
    assert failed.url == f"https://app.dover.com/apply/alloy-robotics/{gone}"
    assert failed.error_type == "JobNotFound"
    assert len(discovered) == len(BOARD_IDS) - 1


def test_a_company_is_only_known_from_a_job_url() -> None:
    with pytest.raises(BoardNotFound, match="job URLs"):
        registry_for(FakeDover()).fetch_board(Platform.DOVER, "alloy-robotics")


@pytest.mark.live
def test_live_job_matches_fixture() -> None:
    """Catches API drift, or the job closing: then re-save the fixtures."""
    with FetcherRegistry() as live:
        posting = live.fetch(parse_url(JOB_URL))
    assert posting.title == "Forward-Deployed Engineer"
    assert posting.raw["client_id"] == CLIENT_ID
    assert posting.description_text
