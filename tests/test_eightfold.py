"""Eightfold fetcher: one job, a search URL as a board, --expand and refresh listing.

Offline, against real Qualcomm (newer "PCSX" API) and Netflix (older API)
responses saved 2026-10. The /careers pages are trimmed to the lines that name
the site's domain, and the search pages lose their 22 KB of filter menus.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.cli import expand_companies, run
from jobfilter.fetchers import BoardNotFound, FetcherRegistry, HttpClient, JobNotFound, RateLimited, TransportFailure
from jobfilter.fetchers.eightfold import COOLDOWN, INTERVAL, MAX_BLOCKED_TRIES
from jobfilter.models import Label, Origin, Platform, Status
from jobfilter.roles import location_kind
from jobfilter.url_parser import parse_url

from .conftest import FIXTURES

EF = FIXTURES / "eightfold"
QC = "https://careers.qualcomm.com"
NF = "https://explore.jobs.netflix.net"
JOB_ID = "446720880729"
CITIZEN_JOB_ID = "446720740540"
NF_JOB_ID = "790318399834"
# The two URLs the request came with: the older API's search, and one job.
SEARCH_URL = f"{QC}/api/apply/v2/jobs?domain=qualcomm.com&query=robotics&location=United%20States&start=0&num=10"
JOB_API_URL = f"{QC}/api/apply/v2/jobs/{JOB_ID}?domain=qualcomm.com"
JOB_URL = f"{QC}/careers/job/{JOB_ID}"
PAGES = {host: (EF / f"{name}_careers_page.html").read_text() for host, name in ((QC, "qualcomm"), (NF, "netflix"))}
QC_SEARCH = {start: json.loads((EF / f"qualcomm_search_robotics_{start}.json").read_text()) for start in range(0, 50, 10)}
QC_SEARCH_IDS = [str(p["id"]) for start in sorted(QC_SEARCH) for p in QC_SEARCH[start]["data"]["positions"]]
NF_SEARCH = json.loads((EF / "netflix_v2_search_robotics_0.json").read_text())
TEMPLATE = json.loads((EF / f"qualcomm_job_{JOB_ID}.json").read_text())


def fixture(name: str) -> object:
    return json.loads((EF / name).read_text())


class FakeEightfold(httpx.MockTransport):
    """Serves the saved sites. Qualcomm's whole board is the saved robotics
    search; jobs in it without a file of their own are the saved Qualcomm job
    under their id."""

    def __init__(
        self,
        missing: frozenset[str] = frozenset(),
        flaky: dict[str, int] | None = None,
        empty_board: bool = False,
        firewall: int = 0,
        firewall_after: int = 0,
    ) -> None:
        self.requests: list[str] = []
        self._missing = missing
        self._flaky = dict(flaky or {})
        self._empty_board = empty_board
        # Refuse this many requests, as Qualcomm's AWS firewall does, once
        # `firewall_after` have been answered.
        self._firewall = firewall
        self._firewall_after = firewall_after
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if self._firewall and len(self.requests) > self._firewall_after:
            self._firewall -= 1
            return httpx.Response(
                405, text="<title>Human Verification</title>", headers={"x-amzn-waf-action": "captcha"}
            )
        site = f"{request.url.scheme}://{request.url.host}"
        params = request.url.params
        path = request.url.path
        if path == "/careers":
            if site in PAGES:
                return httpx.Response(200, text=PAGES[site], headers={"content-type": "text/html"})
            return httpx.Response(200, text="<html><title>Some other careers page</title></html>")
        if path == "/api/pcsx/position_details":
            return self._job(params["position_id"])
        if path == "/api/pcsx/search":
            if site == NF:
                return httpx.Response(403, json=fixture("netflix_pcsx_search_refused.json"))
            if site == QC and not (self._empty_board and "query" not in params):
                start = int(params["start"])
                return httpx.Response(200, json=QC_SEARCH.get(start, {"status": 200, "data": {"positions": [], "count": 44}}))
            return httpx.Response(200, json={"status": 200, "data": {"positions": [], "count": 0}})
        if path == "/api/apply/v2/jobs":
            if site == QC:
                return httpx.Response(403, json=fixture("qualcomm_v2_search_refused.json"))
            if site == NF and params["start"] == "0":
                return httpx.Response(200, json=NF_SEARCH)
            return httpx.Response(200, json={**NF_SEARCH, "positions": []})
        return httpx.Response(404, text="Not Found")

    def _job(self, job_id: str) -> httpx.Response:
        if self._flaky.get(job_id):
            self._flaky[job_id] -= 1
            return httpx.Response(200, text="Please try again later")
        name = "netflix" if job_id == NF_JOB_ID else "qualcomm"
        saved = EF / f"{name}_job_{job_id}.json"
        if job_id in self._missing:
            return httpx.Response(404, json=fixture("qualcomm_job_missing.json"))
        if saved.exists():
            return httpx.Response(200, json=json.loads(saved.read_text()))
        if job_id in QC_SEARCH_IDS:
            data = {**TEMPLATE["data"], "id": int(job_id), "name": f"Job {job_id}"}
            return httpx.Response(200, json={**TEMPLATE, "data": data})
        return httpx.Response(404, json=fixture("qualcomm_job_missing.json"))


def registry_for(transport: FakeEightfold, sleep=lambda _: None) -> FetcherRegistry:
    return FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=sleep))


def job_requests(transport: FakeEightfold) -> list[str]:
    return [u for u in transport.requests if "/position_details" in u]


@pytest.mark.parametrize("url", [JOB_URL, JOB_API_URL, f"{QC}/careers?query=robotics&pid={JOB_ID}"])
def test_a_job_url_becomes_a_posting(url: str) -> None:
    transport = FakeEightfold()
    with registry_for(transport) as registry:
        posting = registry.fetch(parse_url(url))

    assert transport.requests[0].startswith(f"{QC}/careers"), "the site's page first: it names the domain"
    assert job_requests(transport) == [
        f"{QC}/api/pcsx/position_details?domain=qualcomm.com&position_id={JOB_ID}&hl=en"
    ]
    assert (posting.platform, posting.company_slug, posting.job_id) == (Platform.EIGHTFOLD, "qualcomm", JOB_ID)
    assert posting.title == "Software Engineer, Robotics Simulation & AI Infrastructure"
    assert posting.location == "San Diego, California, United States of America"
    assert location_kind(posting.location) == "us"
    assert posting.url == JOB_URL, "the job's own page, whichever address was pasted"
    assert parse_url(posting.url).slug == posting.company_slug
    assert posting.posted_ago == "Posted 2026-09-17"


def test_the_text_is_the_whole_description_as_plain_text() -> None:
    with registry_for(FakeEightfold()) as registry:
        text = registry.fetch(parse_url(JOB_URL)).description_text
    assert text.startswith("Company:\nQualcomm Technologies, Inc.")
    assert "Minimum Qualifications" in text
    assert "<h2>" not in text and "&gt;" not in text, "HTML is rendered, not copied"


def test_a_citizens_only_job_is_restricted() -> None:
    with registry_for(FakeEightfold()) as registry:
        results = run([f"{QC}/careers/job/{CITIZEN_JOB_ID}"], registry, RuleClassifier())
    assert [(r.status, r.platform, r.company_slug) for r in results] == [(Status.OK, Platform.EIGHTFOLD, "qualcomm")]
    assert results[0].label is Label.RESTRICTED_CITIZEN
    assert "U.S. citizen" in results[0].evidence


def test_the_site_page_is_read_once_per_run() -> None:
    transport = FakeEightfold()
    with registry_for(transport) as registry:
        registry.fetch(parse_url(JOB_URL))
        registry.fetch(parse_url(f"{QC}/careers/job/{CITIZEN_JOB_ID}"))
        registry.fetch(parse_url(JOB_URL))
    assert [u for u in transport.requests if "/careers" in u and "/api/" not in u] == [f"{QC}/careers"]
    assert len(job_requests(transport)) == 2, "a job already fetched is not fetched again"


def test_a_job_that_is_gone_is_reported() -> None:
    with registry_for(FakeEightfold(missing=frozenset({JOB_ID}))) as registry:
        with pytest.raises(JobNotFound, match=JOB_ID):
            registry.fetch(parse_url(JOB_URL))


def test_a_site_that_is_not_eightfold_says_so() -> None:
    with registry_for(FakeEightfold()) as registry:
        with pytest.raises(BoardNotFound, match="does not look like an Eightfold career site"):
            registry.fetch(parse_url("https://careers.example.com/careers/job/123456789012"))


def test_a_search_url_lists_then_fetches_each_job() -> None:
    transport = FakeEightfold()
    with registry_for(transport) as registry:
        results = run([SEARCH_URL], registry, RuleClassifier())

    searches = [u for u in transport.requests if "/search" in u or "/apply/v2/jobs?" in u]
    assert sorted(searches) == sorted(
        f"{QC}/api/pcsx/search?domain=qualcomm.com&query=robotics&location=United+States&start={start}"
        for start in range(0, 50, 10)
    ), "the newer search API, with the URL's query and location, ten jobs a page"
    assert sorted(r.job_id for r in results) == sorted(QC_SEARCH_IDS)
    assert len(QC_SEARCH_IDS) == 44
    assert {(r.status, r.origin) for r in results} == {(Status.OK, Origin.DISCOVERED)}
    assert {r.label for r in results if r.job_id == CITIZEN_JOB_ID} == {Label.RESTRICTED_CITIZEN}


def test_an_older_site_is_searched_through_the_older_api() -> None:
    transport = FakeEightfold()
    with registry_for(transport) as registry:
        results = run([f"{NF}/careers?query=robotics"], registry, RuleClassifier())
        registry.list_board(Platform.EIGHTFOLD, "netflix")

    assert [(r.status, r.company_slug, r.job_id) for r in results] == [(Status.OK, "netflix", NF_JOB_ID)]
    assert results[0].title == "Creative Tech Researcher 4"
    pcsx = [u for u in transport.requests if "/api/pcsx/search" in u]
    assert len(pcsx) == 1, "refused once, then the site is known to use the older API"


def test_a_job_expands_to_the_rest_of_its_company() -> None:
    transport = FakeEightfold()
    with registry_for(transport) as registry:
        results = run([JOB_URL], registry, RuleClassifier())
        found = expand_companies(results, registry, RuleClassifier())

    assert sorted(r.job_id for r in found) == sorted(i for i in QC_SEARCH_IDS if i != JOB_ID)
    assert job_requests(transport).count(
        f"{QC}/api/pcsx/position_details?domain=qualcomm.com&position_id={JOB_ID}&hl=en"
    ) == 1, "the job already fetched is not fetched again"


def test_a_board_is_listed_without_fetching_its_jobs() -> None:
    transport = FakeEightfold()
    with registry_for(transport) as registry:
        registry.learn(parse_url(JOB_URL))
        assert transport.requests == [], "learning a site costs nothing"
        refs = registry.list_board(Platform.EIGHTFOLD, "qualcomm")

    assert job_requests(transport) == [], "listing fetches no job"
    assert sorted(ref.job_id for ref in refs) == sorted(QC_SEARCH_IDS)
    assert refs[0].url == f"{QC}/careers/job/{refs[0].job_id}"
    assert parse_url(refs[0].url) == refs[0], "each reference is a URL fetch accepts"
    assert registry.lists_boards(Platform.EIGHTFOLD), "a refresh lists first, then fetches what is new"


def test_a_board_that_lists_nothing_is_missing_not_empty() -> None:
    """A refresh must not read an empty listing as 'every stored job was closed'."""
    with registry_for(FakeEightfold(empty_board=True)) as registry:
        registry.learn(parse_url(JOB_URL))
        with pytest.raises(BoardNotFound, match="lists no jobs"):
            registry.list_board(Platform.EIGHTFOLD, "qualcomm")


def test_a_company_never_seen_has_no_board() -> None:
    with registry_for(FakeEightfold()) as registry:
        with pytest.raises(BoardNotFound, match="found from one of its job or search URLs"):
            registry.fetch_board(Platform.EIGHTFOLD, "qualcomm")


def test_one_failed_job_does_not_lose_the_search() -> None:
    gone = QC_SEARCH_IDS[3]
    with registry_for(FakeEightfold(missing=frozenset({gone}))) as registry:
        results = run([SEARCH_URL], registry, RuleClassifier())
    by_id = {r.job_id: r for r in results}
    assert len(results) == 44, "the failed job is a row, not a gap"
    assert by_id[gone].status is Status.FETCH_ERROR and by_id[gone].error_type == "JobNotFound"


def test_a_job_the_site_turned_away_is_tried_again_after_the_rest() -> None:
    """'Please try again later' arrives as HTTP 200, which the client does not retry."""
    flaky = QC_SEARCH_IDS[5]
    with registry_for(FakeEightfold(flaky={flaky: 1})) as registry:
        results = run([SEARCH_URL], registry, RuleClassifier())
    assert {r.job_id: r.status for r in results}[flaky] is Status.OK


def test_requests_to_a_host_take_turns_at_a_steady_pace() -> None:
    """Refresh fetches from its own threads, so the pace is kept per host, not per pool."""
    waits: list[float] = []
    transport = FakeEightfold()
    with registry_for(transport, sleep=waits.append) as registry:
        run([SEARCH_URL], registry, RuleClassifier())

    turns = sorted(waits)
    assert len(turns) == len(transport.requests) == 1 + 5 + 44
    # The sleeps are no-ops here, so each turn is booked INTERVAL after the one before.
    gaps = [later - earlier for earlier, later in zip(turns, turns[1:])]
    assert sum(gaps) / len(gaps) == pytest.approx(INTERVAL, abs=0.05)


def test_a_firewall_refusal_holds_the_host_then_goes_on_slower() -> None:
    waits: list[float] = []
    transport = FakeEightfold(firewall=3, firewall_after=10)
    with registry_for(transport, sleep=waits.append) as registry:
        results = run([SEARCH_URL], registry, RuleClassifier())
        pace = registry.fetcher_for(Platform.EIGHTFOLD)._pace("careers.qualcomm.com")

    assert {r.status for r in results} == {Status.OK}, "refused requests are sent again, not failed"
    assert len(results) == 44
    assert max(waits) >= COOLDOWN - 1, "every request waits out the hold"
    assert pace.interval == 2 * max(INTERVAL, 0.5), "then at half the pace, halved once per hold"


def test_a_firewall_that_never_relents_gives_the_host_up() -> None:
    transport = FakeEightfold(firewall=10_000, firewall_after=10)
    with registry_for(transport) as registry:
        results = run([SEARCH_URL], registry, RuleClassifier())

    assert len(results) == 44, "every job is still a row"
    failed = [r for r in results if r.status is Status.FETCH_ERROR]
    assert failed and {r.error_type for r in failed} == {"RateLimited"}
    assert "captcha" in failed[0].error
    # The jobs in flight when it was given up made their tries; no job after that was sent.
    assert len(transport.requests) < 10 + 4 * MAX_BLOCKED_TRIES


def test_a_firewall_refusal_of_one_job_is_rate_limited_not_missing() -> None:
    with registry_for(FakeEightfold(firewall=10_000, firewall_after=1)) as registry:
        with pytest.raises(RateLimited, match="careers.qualcomm.com"):
            registry.fetch(parse_url(JOB_URL))


def test_a_search_both_apis_refuse_is_a_transport_failure() -> None:
    refused = httpx.Response(403, json={"message": "Not authorized"})

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/careers":
            return httpx.Response(200, text=PAGES[QC])
        return refused

    client = HttpClient(transport=httpx.MockTransport(handle), backoff=0.0, sleep=lambda _: None)
    with FetcherRegistry(client) as registry:
        with pytest.raises(TransportFailure, match="Not authorized"):
            registry.fetch_board_url(parse_url(SEARCH_URL))


@pytest.mark.live
def test_the_real_sites_still_answer() -> None:
    with FetcherRegistry() as registry:
        posting = registry.fetch(parse_url(JOB_API_URL))
        found = registry.fetch_board_url(parse_url(f"{NF}/careers?query=engineer"))
    assert posting.title and posting.description_text
    assert found and all(not isinstance(p, Exception) for p in found[:5])
