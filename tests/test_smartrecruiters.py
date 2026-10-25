"""SmartRecruiters fetcher: one job, a company's board, board URLs and --expand.

Offline, against real Intuitive responses saved 2026-10. The saved board is
trimmed to three jobs, and the recruiter named as each posting's creator is removed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.cli import expand_companies, run
from jobfilter.fetchers import BoardNotFound, FetcherRegistry, HttpClient, JobNotFound, TransportFailure
from jobfilter.models import Label, Origin, Platform, Status
from jobfilter.roles import location_kind
from jobfilter.storage.store import company_name
from jobfilter.url_parser import parse_url

from .conftest import FIXTURES

SR = FIXTURES / "smartrecruiters"
API = "https://api.smartrecruiters.com/v1/companies/intuitive/postings"
JOB_ID = "744000153260889"
JOB_URL = f"https://jobs.smartrecruiters.com/Intuitive/{JOB_ID}-robotic-algorithm-and-controls-engineer-?oga=true"
BOARD_URL = "https://jobs.smartrecruiters.com/Intuitive"
BOARD = json.loads((SR / "board_intuitive.json").read_text())
EMPTY = json.loads((SR / "board_empty.json").read_text())
BOARD_IDS = [entry["id"] for entry in BOARD["content"]]


class FakeSmartRecruiters(httpx.MockTransport):
    def __init__(self, missing: frozenset[str] = frozenset(), pages: dict[int, dict] | None = None) -> None:
        self.requests: list[str] = []
        self._missing = missing
        self._pages = pages
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url.startswith(API + "?"):
            offset = int(request.url.params["offset"])
            if self._pages is not None:
                return httpx.Response(200, json=self._pages.get(offset, {**EMPTY, "offset": offset}))
            return httpx.Response(200, json=BOARD if offset == 0 else EMPTY)
        job_id = url.removeprefix(API + "/")
        path = SR / f"job_intuitive_{job_id}.json"
        if url.startswith(API + "/") and job_id not in self._missing and path.exists():
            return httpx.Response(200, json=json.loads(path.read_text()))
        if url.startswith("https://api.smartrecruiters.com/v1/companies/") and "?" in url:
            return httpx.Response(200, json=EMPTY)  # an unknown company: an empty list, not a 404
        return httpx.Response(404, json={"httpCode": 404, "code": "RESOURCE_NOT_FOUND"})


def registry_for(transport: FakeSmartRecruiters) -> FetcherRegistry:
    return FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None))


def test_a_job_url_becomes_a_posting() -> None:
    transport = FakeSmartRecruiters()
    with registry_for(transport) as registry:
        posting = registry.fetch(parse_url(JOB_URL))

    assert transport.requests == [f"{API}/{JOB_ID}"], "one request, straight to the job"
    assert (posting.platform, posting.company_slug, posting.job_id) == (Platform.SMARTRECRUITERS, "intuitive", JOB_ID)
    assert posting.title == "Robotic Algorithm and Controls Engineer"
    assert posting.location == "Sunnyvale, CA, United States" and location_kind(posting.location) == "us"
    assert posting.url == f"https://jobs.smartrecruiters.com/Intuitive/{JOB_ID}-robotic-algorithm-and-controls-engineer-", (
        "the job's own address, without the pasted link's tracking query"
    )
    assert posting.posted_ago == "Posted 2026-10-02"
    assert company_name(posting) == "Intuitive"


def test_the_api_address_works_as_a_job_url_too() -> None:
    with registry_for(FakeSmartRecruiters()) as registry:
        posting = registry.fetch(parse_url(f"https://api.smartrecruiters.com/v1/companies/Intuitive/postings/{JOB_ID}"))
    assert posting.job_id == JOB_ID


def test_every_section_of_the_ad_is_kept_under_its_title() -> None:
    with registry_for(FakeSmartRecruiters()) as registry:
        text = registry.fetch(parse_url(JOB_URL)).description_text

    order = [text.index(title) for title in ("Company Description", "Job Description", "Qualifications", "Additional Information")]
    assert order == sorted(order)
    assert "Knowledge of classical and modern servo co" in text
    assert "<p>" not in text and "&#xa0;" not in text, "HTML is rendered, not copied"


def test_it_classifies_like_any_other_posting() -> None:
    with registry_for(FakeSmartRecruiters()) as registry:
        results = run([JOB_URL], registry, RuleClassifier())
    assert [(r.status, r.platform, r.company_slug) for r in results] == [(Status.OK, Platform.SMARTRECRUITERS, "intuitive")]
    assert results[0].label in set(Label)


def test_a_job_that_is_gone_is_reported() -> None:
    with registry_for(FakeSmartRecruiters()) as registry:
        with pytest.raises(JobNotFound, match="999999999999"):
            registry.fetch(parse_url("https://jobs.smartrecruiters.com/Intuitive/999999999999-gone"))


def test_a_board_url_lists_then_fetches_each_job() -> None:
    transport = FakeSmartRecruiters()
    with registry_for(transport) as registry:
        results = run([BOARD_URL], registry, RuleClassifier())

    assert transport.requests[0] == f"{API}?offset=0&limit=100"
    assert sorted(transport.requests[1:]) == sorted(f"{API}/{job_id}" for job_id in BOARD_IDS)
    assert [r.job_id for r in results] == BOARD_IDS
    assert {(r.status, r.origin) for r in results} == {(Status.OK, Origin.DISCOVERED)}
    assert {location_kind(r.location) for r in results} == {"us", "outside_us"}


def test_a_long_board_is_listed_a_hundred_at_a_time() -> None:
    template = BOARD["content"][0]
    ids = [str(700_000_000_000 + i) for i in range(250)]
    pages = {
        offset: {"offset": offset, "limit": 100, "totalFound": 250,
                 "content": [{**template, "id": i} for i in ids[offset:offset + 100]]}
        for offset in (0, 100, 200)
    }
    transport = FakeSmartRecruiters(pages=pages)
    with registry_for(transport) as registry:
        refs = registry.list_board(Platform.SMARTRECRUITERS, "intuitive")

    assert transport.requests == [f"{API}?offset={o}&limit=100" for o in (0, 100, 200)], "listing fetches no job"
    assert [ref.job_id for ref in refs] == ids
    assert refs[0].url == f"https://jobs.smartrecruiters.com/intuitive/{ids[0]}"
    assert parse_url(refs[0].url) == refs[0], "each reference is a URL fetch accepts"


def test_a_job_expands_to_the_rest_of_its_company() -> None:
    transport = FakeSmartRecruiters()
    with registry_for(transport) as registry:
        results = run([JOB_URL], registry, RuleClassifier())
        found = expand_companies(results, registry, RuleClassifier())

    assert [r.job_id for r in found] == [i for i in BOARD_IDS if i != JOB_ID]
    assert transport.requests.count(f"{API}/{JOB_ID}") == 1, "the job already fetched is not fetched again"


def test_one_failed_job_does_not_lose_the_board() -> None:
    gone = BOARD_IDS[1]
    with registry_for(FakeSmartRecruiters(missing=frozenset({gone}))) as registry:
        results = run([BOARD_URL], registry, RuleClassifier())
    by_id = {r.job_id: r for r in results}
    assert len(results) == len(BOARD_IDS), "the failed job is a row, not a gap"
    assert by_id[gone].status is Status.FETCH_ERROR and by_id[gone].error_type == "JobNotFound"
    assert by_id[JOB_ID].status is Status.OK


def test_an_unknown_company_is_a_missing_board_not_an_empty_one() -> None:
    """SmartRecruiters answers 200 with no postings. A refresh must not read that as 'all closed'."""
    with registry_for(FakeSmartRecruiters()) as registry:
        with pytest.raises(BoardNotFound, match="nosuchcompany"):
            registry.fetch_board(Platform.SMARTRECRUITERS, "nosuchcompany")


def test_a_list_that_is_not_json_is_a_transport_failure() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>maintenance</html>"))
    with FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None)) as registry:
        with pytest.raises(TransportFailure):
            registry.fetch_board(Platform.SMARTRECRUITERS, "intuitive")


def test_smartrecruiters_boards_can_be_listed_for_a_quick_refresh() -> None:
    with registry_for(FakeSmartRecruiters()) as registry:
        assert registry.supports_boards(Platform.SMARTRECRUITERS)
        assert registry.lists_boards(Platform.SMARTRECRUITERS)


@pytest.mark.live
def test_the_real_api_still_answers() -> None:
    with FetcherRegistry() as registry:
        refs = registry.list_board(Platform.SMARTRECRUITERS, "intuitive")
        posting = registry.fetch(refs[0])
    assert posting.title and posting.description_text
