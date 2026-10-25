"""Workable fetcher: careers-site links, job-board links, short links and boards.

Offline, against real Octavian Robotics responses saved 2026-10.
"""

from __future__ import annotations

import copy
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

WK = FIXTURES / "workable"
ACCOUNT = "octavian-robotics"
SHORTCODE = "3FF60B88A4"
OTHER = "C3BF121EEB"
VIEW_ID = "nPbcR1G5FfkYVf2cezffrL"
BOARD_API = f"https://apply.workable.com/api/v1/widget/accounts/{ACCOUNT}?details=true"
VIEW_API = f"https://jobs.workable.com/api/v1/jobs/{VIEW_ID}"
SHORT_LINK = f"https://apply.workable.com/j/{SHORTCODE}"
JOB_URL = f"https://apply.workable.com/{ACCOUNT}/j/{SHORTCODE}/"
VIEW_URL = (
    f"https://jobs.workable.com/view/{VIEW_ID}/"
    "full-stack-robotics-software-engineer-in-los-angeles-at-octavian-robotics?subid=direct"
)
BOARD_URL = f"https://apply.workable.com/{ACCOUNT}/"
BOARD = json.loads((WK / "board_octavian-robotics.json").read_text())
VIEW = json.loads((WK / f"view_{VIEW_ID}.json").read_text())


class FakeWorkable(httpx.MockTransport):
    """Serves the saved answers; anything else is a 404, as on the real site."""

    def __init__(self, answers: dict[str, httpx.Response] | None = None) -> None:
        self.requests: list[str] = []
        self._answers = {
            BOARD_API: httpx.Response(200, json=BOARD),
            VIEW_API: httpx.Response(200, json=VIEW),
            SHORT_LINK: httpx.Response(301, headers={"location": f"https://apply.workable.com/{ACCOUNT}/j/{SHORTCODE}"}),
            **(answers or {}),
        }
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        return self._answers.get(url, httpx.Response(404, text="Not Found"))


def registry_for(transport: FakeWorkable) -> FetcherRegistry:
    return FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None))


# --------------------------------------------------------------------------
# The careers site
# --------------------------------------------------------------------------

def test_a_job_url_becomes_a_posting() -> None:
    transport = FakeWorkable()
    with registry_for(transport) as registry:
        posting = registry.fetch(parse_url(JOB_URL))

    assert transport.requests == [BOARD_API], "one request: the account's board holds the job and its text"
    assert (posting.platform, posting.company_slug, posting.job_id) == (Platform.WORKABLE, ACCOUNT, SHORTCODE)
    assert posting.title == "Full Stack Robotics Software Engineer"
    assert posting.location == "Los Angeles, California, United States" and location_kind(posting.location) == "us"
    assert posting.url == f"https://apply.workable.com/{ACCOUNT}/j/{SHORTCODE}"
    assert posting.posted_ago == "Posted 2026-09-16"
    assert company_name(posting) == "Octavian Robotics"


def test_the_text_holds_description_requirements_and_benefits() -> None:
    with registry_for(FakeWorkable()) as registry:
        text = registry.fetch(parse_url(JOB_URL)).description_text
    assert text.startswith("Overview")
    assert "Must be AI-Native in all workflows" in text, "requirements"
    assert "Relocation package available" in text, "benefits"
    assert "<li>" not in text and "<p>" not in text


def test_two_jobs_of_one_company_cost_one_request() -> None:
    transport = FakeWorkable()
    with registry_for(transport) as registry:
        registry.fetch(parse_url(JOB_URL))
        other = registry.fetch(parse_url(f"https://apply.workable.com/api/v2/accounts/{ACCOUNT}/jobs/{OTHER}"))
    assert other.title == "Mechatronics Engineer (Lead)"
    assert transport.requests == [BOARD_API]


def test_a_short_link_finds_its_account_from_the_redirect() -> None:
    transport = FakeWorkable()
    with registry_for(transport) as registry:
        posting = registry.fetch(parse_url(SHORT_LINK))
    assert transport.requests == [SHORT_LINK, BOARD_API]
    assert (posting.company_slug, posting.job_id) == (ACCOUNT, SHORTCODE)


# --------------------------------------------------------------------------
# Workable's job board
# --------------------------------------------------------------------------

def test_a_job_board_link_is_stored_as_the_careers_site_job() -> None:
    """Two links to one job must give one posting, whichever was pasted."""
    transport = FakeWorkable()
    with registry_for(transport) as registry:
        from_board = registry.fetch(parse_url(VIEW_URL))
        from_site = registry.fetch(parse_url(JOB_URL))

    assert transport.requests == [VIEW_API, BOARD_API]
    assert from_board == from_site
    assert (from_board.company_slug, from_board.job_id) == (ACCOUNT, SHORTCODE)


def test_two_openings_with_one_title_are_told_apart_by_city() -> None:
    board = copy.deepcopy(BOARD)
    twin = copy.deepcopy(next(j for j in board["jobs"] if j["shortcode"] == SHORTCODE))
    twin.update(shortcode="AAAAAAAAAA", city="Austin", state="Texas",
                locations=[{"country": "United States", "countryCode": "US", "city": "Austin", "region": "Texas"}])
    board["jobs"].insert(0, twin)
    with registry_for(FakeWorkable({BOARD_API: httpx.Response(200, json=board)})) as registry:
        posting = registry.fetch(parse_url(VIEW_URL))
    assert posting.job_id == SHORTCODE, "the Los Angeles one, as the job-board link says"


@pytest.mark.parametrize(
    "board_answer",
    [httpx.Response(404, text="Not Found"), httpx.Response(200, json={**BOARD, "jobs": [BOARD["jobs"][1]]})],
    ids=["no-careers-site-under-that-name", "not-listed-there"],
)
def test_a_job_board_link_that_cannot_be_matched_is_still_returned(board_answer: httpx.Response) -> None:
    with registry_for(FakeWorkable({BOARD_API: board_answer})) as registry:
        results = run([VIEW_URL], registry, RuleClassifier())

    posting = results[0].posting
    assert results[0].status is Status.OK and results[0].label in set(Label)
    assert (posting.company_slug, posting.job_id) == (ACCOUNT, VIEW_ID), "filed under the view id"
    assert posting.title == "Full Stack Robotics Software Engineer"
    assert posting.location == "Los Angeles, California, United States"
    assert "Must be AI-Native in all workflows" in posting.description_text
    assert "Relocation package available" in posting.description_text
    assert company_name(posting) == "Octavian Robotics"
    assert posting.url.startswith(f"https://jobs.workable.com/view/{VIEW_ID}/")


# --------------------------------------------------------------------------
# Boards
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [BOARD_URL, f"https://apply.workable.com/api/v1/widget/accounts/{ACCOUNT}",
     f"https://jobs.workable.com/company/4mLRJcLgYqAZkZBBoPcp9u/jobs-at-{ACCOUNT}"],
    ids=["careers-site", "api-address", "job-board-company-page"],
)
def test_a_board_url_gives_every_job(url: str) -> None:
    transport = FakeWorkable()
    with registry_for(transport) as registry:
        results = run([url], registry, RuleClassifier())
    assert transport.requests == [BOARD_API]
    assert [r.job_id for r in results] == [SHORTCODE, OTHER]
    assert {(r.status, r.origin, r.company_slug) for r in results} == {(Status.OK, Origin.DISCOVERED, ACCOUNT)}


def test_a_job_board_link_expands_to_the_rest_of_the_company() -> None:
    transport = FakeWorkable()
    with registry_for(transport) as registry:
        results = run([VIEW_URL], registry, RuleClassifier())
        found = expand_companies(results, registry, RuleClassifier())
    assert [r.job_id for r in found] == [OTHER]
    assert transport.requests == [VIEW_API, BOARD_API], "the board was already in hand"


def test_workable_boards_arrive_whole() -> None:
    with registry_for(FakeWorkable()) as registry:
        assert registry.supports_boards(Platform.WORKABLE)
        assert not registry.lists_boards(Platform.WORKABLE), "one request holds every job's text"


def test_a_remote_job_says_so() -> None:
    board = copy.deepcopy(BOARD)
    board["jobs"][0]["telecommuting"] = True
    with registry_for(FakeWorkable({BOARD_API: httpx.Response(200, json=board)})) as registry:
        posting = registry.fetch(parse_url(JOB_URL))
    assert posting.location == "Remote - Los Angeles, California, United States"


# --------------------------------------------------------------------------
# What can go wrong
# --------------------------------------------------------------------------

def test_an_unknown_account_is_a_missing_board() -> None:
    with registry_for(FakeWorkable()) as registry:
        with pytest.raises(BoardNotFound, match="nosuchcompany"):
            registry.fetch_board(Platform.WORKABLE, "nosuchcompany")


@pytest.mark.parametrize(
    "url",
    [f"https://apply.workable.com/{ACCOUNT}/j/0000000000", "https://apply.workable.com/j/0000000000",
     "https://jobs.workable.com/view/zzzzzzzzzzzzzzzzzzzzzz/gone"],
    ids=["not-on-the-board", "short-link-to-nothing", "job-board-link-to-nothing"],
)
def test_a_job_that_is_gone_is_reported(url: str) -> None:
    with registry_for(FakeWorkable()) as registry:
        with pytest.raises(JobNotFound):
            registry.fetch(parse_url(url))


def test_a_board_answer_of_the_wrong_shape_is_a_transport_failure() -> None:
    with registry_for(FakeWorkable({BOARD_API: httpx.Response(200, json={"jobs": "soon"})})) as registry:
        with pytest.raises(TransportFailure):
            registry.fetch(parse_url(JOB_URL))


@pytest.mark.live
def test_the_real_site_still_answers() -> None:
    with FetcherRegistry() as registry:
        postings = registry.fetch_board(Platform.WORKABLE, "andromeda-robotics")
    assert postings and all(p.title and p.description_text for p in postings)
