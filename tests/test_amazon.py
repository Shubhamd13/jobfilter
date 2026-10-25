"""Amazon Jobs fetcher: one job by its number, and a team as a board.

Offline, against real responses saved 2026-10. The saved team page is trimmed to three jobs.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.cli import expand_companies, run
from jobfilter.fetchers import BoardNotFound, FetcherRegistry, HttpClient, JobNotFound, TransportFailure
from jobfilter.fetchers.amazon import team_slug
from jobfilter.models import Label, Origin, Platform, Status
from jobfilter.storage import Store
from jobfilter.roles import location_kind
from jobfilter.storage.store import company_name
from jobfilter.url_parser import parse_url

from .conftest import FIXTURES

AZ = FIXTURES / "amazon"
JOB_ID = "10568411"
JOB_URL = (
    "https://www.amazon.jobs/en/jobs/10568411/software-development-engineer-amazon-robotics"
    "?cmpid=SPLICX0248M&utm_source=linkedin.com&utm_campaign=cxro&ss=paid"
)
SEARCH = "https://www.amazon.jobs/en/search.json?base_query={}&result_limit=10"
TEAM = "https://www.amazon.jobs/en/search.json?team_category%5B%5D={}&result_limit=100&offset={}"
TEAM_URL = "https://www.amazon.jobs/en/teams/amazon-robotics"
FOUND = json.loads((AZ / "search_10568411.json").read_text())
MISSING = json.loads((AZ / "search_missing.json").read_text())
TEAM_PAGE = json.loads((AZ / "team_amazon-robotics.json").read_text())
TEAM_IDS = [job["id_icims"] for job in TEAM_PAGE["jobs"]]


def team_of(n: int, team: str = "team-amazon-robotics") -> dict[str, httpx.Response]:
    """A team of `n` jobs as the search serves it: pages of a hundred, cut from one real job."""
    template = FOUND["jobs"][0]
    jobs = [{**template, "id_icims": str(30_000_000 + i), "title": f"Job {i}"} for i in range(n)]
    pages = {}
    for offset in range(0, max(n, 1), 100):
        pages[TEAM.format(team, offset)] = httpx.Response(200, json={**FOUND, "hits": n, "jobs": jobs[offset:offset + 100]})
    return pages


class FakeAmazon(httpx.MockTransport):
    def __init__(self, answers: dict[str, httpx.Response] | None = None) -> None:
        self.requests: list[str] = []
        self._answers = answers or {}
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self._answers:
            return self._answers[url]
        if url == SEARCH.format(JOB_ID):
            return httpx.Response(200, json=FOUND)
        if url == TEAM.format("team-amazon-robotics", 0):
            return httpx.Response(200, json=TEAM_PAGE)
        return httpx.Response(200, json=MISSING)


def registry_for(transport: FakeAmazon) -> FetcherRegistry:
    return FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None))


def test_a_job_url_becomes_a_posting() -> None:
    transport = FakeAmazon()
    with registry_for(transport) as registry:
        posting = registry.fetch(parse_url(JOB_URL))

    assert transport.requests == [SEARCH.format(JOB_ID)], "one request, by the job's number"
    assert (posting.platform, posting.company_slug, posting.job_id) == (Platform.AMAZON, "amazon-robotics", JOB_ID), (
        "a job is filed under its team"
    )
    assert posting.title == "Software Development Engineer, Amazon Robotics"
    assert posting.url == "https://www.amazon.jobs/en/jobs/10568411/software-development-engineer-amazon-robotics", (
        "the stored URL is the job's own, without the tracking query"
    )
    assert posting.posted_ago == "Posted October 2, 2026"
    assert company_name(posting) == "Amazon.com Services LLC"


def test_the_location_is_read_from_the_job_not_the_normalized_field() -> None:
    """Amazon's `normalized_location` put this Westboro, MA job in Wisconsin."""
    with registry_for(FakeAmazon()) as registry:
        posting = registry.fetch(parse_url(JOB_URL))
    assert posting.location == "Westboro, MA, USA"
    assert location_kind(posting.location) == "us"


def test_the_description_keeps_required_and_preferred_apart() -> None:
    with registry_for(FakeAmazon()) as registry:
        text = registry.fetch(parse_url(JOB_URL)).description_text

    assert text.startswith("You will design and build the full-stack software")
    basic, preferred = text.index("Basic Qualifications"), text.index("Preferred Qualifications")
    assert 0 < basic < preferred
    assert "3+ years of non-internship professional software development experience" in text[basic:preferred]
    assert "<br" not in text and "&amp;" not in text, "HTML is rendered, not copied"


def test_it_classifies_like_any_other_posting() -> None:
    with registry_for(FakeAmazon()) as registry:
        results = run([JOB_URL], registry, RuleClassifier())
    assert [(r.status, r.platform, r.company_slug) for r in results] == [(Status.OK, Platform.AMAZON, "amazon-robotics")]
    assert results[0].label in set(Label)


def test_a_second_fetch_of_the_same_job_costs_no_request() -> None:
    transport = FakeAmazon()
    with registry_for(transport) as registry:
        registry.fetch(parse_url(JOB_URL))
        registry.fetch(parse_url(f"https://account.amazon.jobs/jobs/{JOB_ID}/apply"))
    assert len(transport.requests) == 1


def test_a_job_that_is_gone_is_reported() -> None:
    with registry_for(FakeAmazon()) as registry:
        with pytest.raises(JobNotFound, match="99999999"):
            registry.fetch(parse_url("https://www.amazon.jobs/en/jobs/99999999"))


def test_a_search_hit_for_another_job_is_not_mistaken_for_it() -> None:
    """The search matches text: a different job that mentions the number must not be returned."""
    other = SEARCH.format("20000001")
    transport = FakeAmazon({other: httpx.Response(200, json=FOUND)})
    with registry_for(transport) as registry:
        with pytest.raises(JobNotFound):
            registry.fetch(parse_url("https://www.amazon.jobs/en/jobs/20000001"))


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, json=["not", "an", "object"]), httpx.Response(200, text="<html>blocked</html>")],
    ids=["wrong-shape", "not-json"],
)
def test_an_unusable_answer_is_a_transport_failure(response: httpx.Response) -> None:
    transport = FakeAmazon({SEARCH.format(JOB_ID): response})
    with registry_for(transport) as registry:
        with pytest.raises(TransportFailure):
            registry.fetch(parse_url(JOB_URL))


# --------------------------------------------------------------------------
# A team is a board
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "team,slug",
    [
        ("team-amazon-robotics", "amazon-robotics"),
        ("amazon-robotics", "amazon-robotics"),
        ("team-project-kuiper", "amazon-project-kuiper"),
        ("no-sub-team-listed", "amazon"),
        ("", "amazon"),
        (None, "amazon"),
    ],
)
def test_team_slug(team: str | None, slug: str) -> None:
    assert team_slug(team) == slug


def test_a_team_page_is_a_board() -> None:
    transport = FakeAmazon()
    with registry_for(transport) as registry:
        results = run([TEAM_URL], registry, RuleClassifier())

    assert transport.requests == [TEAM.format("team-amazon-robotics", 0)], "three jobs fit one request"
    assert [r.job_id for r in results] == TEAM_IDS
    assert {(r.status, r.origin, r.company_slug) for r in results} == {(Status.OK, Origin.DISCOVERED, "amazon-robotics")}
    assert all(r.posting.description_text for r in results), "a search hit carries the full text"


def test_a_job_expands_to_the_rest_of_its_team() -> None:
    transport = FakeAmazon()
    with registry_for(transport) as registry:
        assert registry.supports_boards(Platform.AMAZON)
        results = run([JOB_URL], registry, RuleClassifier())
        found = expand_companies(results, registry, RuleClassifier())

    assert [r.job_id for r in found] == TEAM_IDS
    assert JOB_ID not in {r.job_id for r in found}, "the job asked for is not listed twice"


def test_a_large_team_is_read_a_hundred_at_a_time() -> None:
    transport = FakeAmazon(team_of(250))
    with registry_for(transport) as registry:
        postings = registry.fetch_board(Platform.AMAZON, "amazon-robotics")

    assert [u for u in transport.requests] == [TEAM.format("team-amazon-robotics", o) for o in (0, 100, 200)]
    assert len(postings) == 250 and len({p.job_id for p in postings}) == 250


def test_a_team_whose_label_has_no_amazon_prefix_is_still_found() -> None:
    """"amazon-project-kuiper" is the slug of "team-project-kuiper"."""
    transport = FakeAmazon(team_of(2, "team-project-kuiper"))
    with registry_for(transport) as registry:
        postings = registry.fetch_board(Platform.AMAZON, "amazon-project-kuiper")
    assert len(postings) == 2
    assert transport.requests[0] == TEAM.format("team-amazon-project-kuiper", 0), "the plain label is tried first"


@pytest.mark.parametrize(
    "slug,answers,message",
    [
        ("amazon", {}, "too large to read as one board"),
        ("amazon-nonesuch", {}, "lists no jobs for team"),
        ("amazon-sde-primary", {TEAM.format("team-amazon-sde-primary", 0): httpx.Response(200, json={**FOUND, "hits": 5000})}, "5,000 jobs, too many"),
    ],
    ids=["all-of-amazon", "unknown-team", "a-job-family-not-a-team"],
)
def test_a_board_that_cannot_be_read_says_why(slug: str, answers: dict, message: str) -> None:
    with registry_for(FakeAmazon(answers)) as registry:
        with pytest.raises(BoardNotFound, match=message):
            registry.fetch_board(Platform.AMAZON, slug)


def test_a_job_stored_before_its_team_was_known_is_moved_not_duplicated() -> None:
    """Job ids are Amazon-wide: the same number under another slug is the same job."""
    with registry_for(FakeAmazon()) as registry:
        result = run([JOB_URL], registry, RuleClassifier())[0]
    import dataclasses

    old = dataclasses.replace(result, company_slug="amazon", posting=dataclasses.replace(result.posting, company_slug="amazon"))
    with Store(":memory:") as store:
        store.record_run("t", [old])
        store.record_run("t", [result])
        rows = store.postings()
        assert [(r["company_slug"], r["job_id"]) for r in rows] == [("amazon-robotics", JOB_ID)]


@pytest.mark.live
def test_the_real_site_still_answers() -> None:
    """Amazon's search is undocumented: this is what notices if it changes shape."""
    with FetcherRegistry() as registry:
        found = registry.fetcher_for(Platform.AMAZON)._client.get_json(SEARCH.format("robotics"))
    assert isinstance(found, dict) and found["jobs"] and "id_icims" in found["jobs"][0]
