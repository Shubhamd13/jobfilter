"""Workday fetcher: single jobs, board URLs, --expand, the 2000-result cap.

Offline. Real responses saved from Boston Dynamics and NVIDIA (2026-10) are
served by a fake Workday that also checks what it is asked: a facet the site
does not have is a 400, as on the real API. The over-cap board is synthetic,
since a real one would mean thousands of fixtures.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.cli import expand_companies, run
from jobfilter.fetchers import BoardNotFound, FetcherRegistry, HttpClient, JobNotFound
from jobfilter.fetchers.workday import (
    RESULT_CAP,
    WorkdayFetcher,
    _estimated_total,
    _facet_groups,
    _split_facet,
)
from jobfilter.models import Label, Origin, Platform, Status
from jobfilter.url_parser import parse_url, workday_address

from .conftest import FIXTURES

WD = FIXTURES / "workday"

BD_HOST = "bostondynamics.wd1.myworkdayjobs.com"
BD_API = f"https://{BD_HOST}/wday/cxs/bostondynamics/Boston_Dynamics"
BD_BOARD = f"https://{BD_HOST}/en-US/Boston_Dynamics"
BD_JOB = f"https://{BD_HOST}/Boston_Dynamics/job/Waltham-MA/Staff-Full-Stack-Software-Engineer_R2879"
BD_TRADE_BOARD = f"{BD_BOARD}?q=Global%20Trade"
BD_TRADE_JOB = f"https://{BD_HOST}/Boston_Dynamics/job/Waltham-MA/Staff-Global-Trade-Compliance-Specialist_R3130"
# The five jobs the saved "Global Trade" search lists, by path id.
BD_TRADE_IDS = ["R3130", "R3014-1", "R3013", "R3145", "R3002"]

NV_HOST = "nvidia.wd5.myworkdayjobs.com"
NV_API = f"https://{NV_HOST}/wday/cxs/nvidia/NVIDIAExternalCareerSite"
NV_JOB = (
    f"https://{NV_HOST}/en-US/NVIDIAExternalCareerSite/job/"
    "Robotics-Research-Engineer---Robot-Simulation-and-Evaluation_JR2023397"
)


def load(name: str) -> Any:
    return json.loads((WD / name).read_text())


class FakeWorkday(httpx.MockTransport):
    """Serves saved responses. `search` answers POSTs; jobs are GET by path id."""

    def __init__(
        self,
        search: Callable[[str, dict[str, Any]], httpx.Response] | None = None,
        missing: frozenset[str] = frozenset(),
    ) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self._search = search or bd_search
        self._missing = missing
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, url, body))
        if request.method == "POST" and url.endswith("/jobs"):
            return self._search(url, body)
        if request.method == "GET" and "/job/" in url:
            job_id = url.rsplit("_", 1)[-1]
            name = {BD_API: "bostondynamics", NV_API: "nvidia"}.get(url.split("/job/")[0])
            path = WD / f"{name}_job_{job_id}.json"
            if name and job_id not in self._missing and path.exists():
                return httpx.Response(200, json=json.loads(path.read_text()))
            return httpx.Response(404, json={"errorCode": "NOT_FOUND"})
        return httpx.Response(404)

    def count(self, method: str) -> int:
        return sum(1 for m, _, _ in self.requests if m == method)


def bd_search(url: str, body: dict[str, Any]) -> httpx.Response:
    """Boston Dynamics' search, as saved for "Global Trade"."""
    if url != f"{BD_API}/jobs":
        return httpx.Response(422 if "nope" in url else 404)
    if body["limit"] > 20:
        return httpx.Response(400)
    saved = load("bostondynamics_search_global-trade.json")
    known = {param for param, _ in _facet_groups(saved["facets"])}
    if set(body["appliedFacets"]) - known:
        return httpx.Response(400, json={"errorCode": "HTTP_400"})
    if body["searchText"] != "Global Trade" or body["offset"]:
        return httpx.Response(200, json={"total": 0, "jobPostings": [], "facets": saved["facets"]})
    return httpx.Response(200, json=saved)


def registry_for(transport: FakeWorkday) -> FetcherRegistry:
    return FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None))


@pytest.fixture(scope="module")
def clf() -> RuleClassifier:
    return RuleClassifier()


# --------------------------------------------------------------------------
# One job
# --------------------------------------------------------------------------


def test_a_job_url_becomes_a_posting() -> None:
    transport = FakeWorkday()
    posting = registry_for(transport).fetch(parse_url(BD_JOB))

    assert posting.platform is Platform.WORKDAY
    assert posting.company_slug == "bostondynamics"
    assert posting.job_id == "R2879"
    assert posting.title == "Staff Full Stack Software Engineer"
    assert posting.location == "Waltham, MA"
    assert posting.url == BD_JOB
    assert posting.posted_ago and posting.posted_ago.startswith("Posted")
    assert "<p>" not in posting.description_text
    assert "not able to sponsor visas" in posting.description_text
    # The job path from the URL, location segment included, goes to the API.
    assert transport.requests == [
        ("GET", f"{BD_API}/job/Waltham-MA/Staff-Full-Stack-Software-Engineer_R2879", None)
    ]


def test_the_search_pane_url_fetches_the_same_job() -> None:
    pane = f"https://{NV_HOST}/en-US/NVIDIAExternalCareerSite/details/Robotics-Research-Engineer---Robot-Simulation-and-Evaluation_JR2023397?locationHierarchy1=x"
    a = registry_for(FakeWorkday()).fetch(parse_url(pane))
    b = registry_for(FakeWorkday()).fetch(parse_url(NV_JOB))
    assert (a.company_slug, a.job_id, a.title, a.description_text) == (
        b.company_slug, b.job_id, b.title, b.description_text,
    )


def test_the_users_nvidia_job_is_open(clf: RuleClassifier) -> None:
    posting = registry_for(FakeWorkday()).fetch(parse_url(NV_JOB))
    assert posting.job_id == "JR2023397"
    assert posting.location == "US, CA, Santa Clara"
    assert clf.classify(posting).label is Label.OPEN


def test_a_repost_keeps_the_id_in_its_path() -> None:
    """jobReqId says R3014; the path, and the extension, say R3014-1."""
    url = f"https://{BD_HOST}/Boston_Dynamics/job/Waltham-MA/Senior-Technical-Team-Lead--Mechanical-Engineering_R3014-1"
    posting = registry_for(FakeWorkday()).fetch(parse_url(url))
    assert posting.raw["jobReqId"] == "R3014"
    assert posting.job_id == "R3014-1"


def test_a_missing_job_is_job_not_found() -> None:
    url = f"https://{BD_HOST}/Boston_Dynamics/job/Waltham-MA/Gone_R1"
    with pytest.raises(JobNotFound, match="taken down"):
        registry_for(FakeWorkday()).fetch(parse_url(url))


# --------------------------------------------------------------------------
# Board URLs
# --------------------------------------------------------------------------


def test_a_board_url_checks_every_job_it_lists(clf: RuleClassifier) -> None:
    transport = FakeWorkday()
    results = run([BD_TRADE_BOARD], registry_for(transport), clf)

    assert [r.job_id for r in results] == BD_TRADE_IDS
    assert {r.origin for r in results} == {Origin.DISCOVERED}
    assert {r.status for r in results} == {Status.OK}
    assert {r.company_slug for r in results} == {"bostondynamics"}
    # One search page, one request per description.
    assert transport.count("POST") == 1
    assert transport.count("GET") == 5
    # ITAR named as a skill to have is not a restriction on who may apply.
    trade = next(r for r in results if r.job_id == "R3130")
    assert trade.label is Label.UNCLEAR
    assert "ITAR" in (trade.evidence or "")


def test_a_job_given_by_url_and_on_a_board_appears_once(clf: RuleClassifier) -> None:
    results = run([BD_TRADE_BOARD, BD_TRADE_JOB], registry_for(FakeWorkday()), clf)
    ids = [r.job_id for r in results]
    assert sorted(ids) == sorted(BD_TRADE_IDS)
    assert next(r for r in results if r.job_id == "R3130").origin is Origin.REQUESTED


def test_url_parameters_that_are_not_facets_are_dropped() -> None:
    """`source=LinkedIn` would be a 400; it is checked against the site's facets."""
    transport = FakeWorkday()
    results = run([BD_TRADE_BOARD + "&source=LinkedIn"], registry_for(transport), RuleClassifier())
    assert [r.job_id for r in results] == BD_TRADE_IDS
    searches = [body for method, _, body in transport.requests if method == "POST"]
    assert [s["appliedFacets"] for s in searches] == [{}, {}]


def test_a_job_that_fails_is_reported_not_dropped(clf: RuleClassifier) -> None:
    results = run([BD_TRADE_BOARD], registry_for(FakeWorkday(missing=frozenset({"R3013"}))), clf)
    assert len(results) == 5
    [failed] = [r for r in results if r.status is not Status.OK]
    assert failed.error_type == "JobNotFound"
    assert failed.origin is Origin.DISCOVERED
    assert failed.company_slug == "bostondynamics"
    assert failed.label is Label.UNCLEAR


def test_rate_limited_jobs_get_a_second_slower_pass(clf: RuleClassifier) -> None:
    """NVIDIA answered 429 to 26 of 2,654 jobs fetched 6 at a time."""
    limited = {"R3013": 3, "R3145": 9}  # 429s left to serve, per job

    class RateLimited(FakeWorkday):
        def _handle(self, request: httpx.Request) -> httpx.Response:
            job_id = str(request.url).rsplit("_", 1)[-1]
            if request.method == "GET" and limited.get(job_id, 0) > 0:
                limited[job_id] -= 1
                self.requests.append((request.method, str(request.url), None))
                return httpx.Response(429)
            return super()._handle(request)

    results = run([BD_TRADE_BOARD], registry_for(RateLimited()), clf)
    by_id = {r.job_id: r for r in results}
    # R3013: two 429s in the parallel pass (with its one client retry), then fine.
    assert by_id["R3013"].status is Status.OK
    # R3145 never recovers: still reported, with its page and id, not the API call.
    failed = by_id["R3145"]
    assert failed.status is Status.FETCH_ERROR
    assert failed.error_type == "TransportFailure"
    assert failed.url.startswith(f"https://{BD_HOST}/Boston_Dynamics/job/")
    assert "/wday/cxs/" not in failed.url
    assert len(results) == 5


@pytest.mark.parametrize(
    "board,error",
    [
        (f"https://{BD_HOST}/en-US/NoSuchSite", "BoardNotFound"),
        ("https://nope.wd1.myworkdayjobs.com/en-US/Careers", "BoardNotFound"),
    ],
)
def test_a_board_that_does_not_exist_is_one_failed_row(board: str, error: str) -> None:
    def search(url: str, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(422 if "nope" in url else 404)

    [result] = run([board], registry_for(FakeWorkday(search=search)), RuleClassifier())
    assert result.status is Status.FETCH_ERROR
    assert result.error_type == error
    assert result.url == board


def test_an_empty_search_is_reported() -> None:
    [result] = run([f"{BD_BOARD}?q=nothing"], registry_for(FakeWorkday()), RuleClassifier())
    assert result.error_type == "EmptyBoard"


# --------------------------------------------------------------------------
# --expand
# --------------------------------------------------------------------------


def test_expand_finds_the_board_from_a_job_url(clf: RuleClassifier) -> None:
    def search(url: str, body: dict[str, Any]) -> httpx.Response:
        # The whole board, unfiltered: here the five saved jobs.
        assert body["searchText"] == "" and body["appliedFacets"] == {}
        return bd_search(url, {**body, "searchText": "Global Trade"})

    transport = FakeWorkday(search=search)
    registry = registry_for(transport)
    requested = run([BD_TRADE_JOB], registry, clf)
    discovered = expand_companies(requested, registry, clf)

    assert [r.job_id for r in discovered] == [i for i in BD_TRADE_IDS if i != "R3130"]
    assert {r.origin for r in discovered} == {Origin.DISCOVERED}
    # R3130 was fetched once, for the requested URL, and not again.
    assert transport.count("GET") == 5


def test_expand_after_a_board_url_fetches_nothing_twice(clf: RuleClassifier) -> None:
    def search(url: str, body: dict[str, Any]) -> httpx.Response:
        return bd_search(url, {**body, "searchText": "Global Trade"})

    transport = FakeWorkday(search=search)
    registry = registry_for(transport)
    results = run([BD_BOARD], registry, clf)
    before = len(transport.requests)
    assert expand_companies(results, registry, clf) == []
    assert len(transport.requests) == before


def test_a_board_is_only_known_from_a_url() -> None:
    with pytest.raises(BoardNotFound, match="job or board URL"):
        registry_for(FakeWorkday()).fetch_board(Platform.WORKDAY, "bostondynamics")


# --------------------------------------------------------------------------
# Paging and the 2000-result cap (synthetic board)
# --------------------------------------------------------------------------


def synthetic_board(jobs_by_family: dict[str, list[str]], total_without_facets: int):
    """A search endpoint over made-up jobs, with a jobFamilyGroup facet.

    Unfiltered, it claims `total_without_facets` and pages through the union;
    like the real API, `total` is only on the first page.
    """
    every = list(dict.fromkeys(j for jobs in jobs_by_family.values() for j in jobs))
    facets = [
        {
            "facetParameter": "jobFamilyGroup",
            "values": [{"id": f, "count": len(j), "descriptor": f} for f, j in jobs_by_family.items()],
        },
        {
            "facetParameter": "timeType",
            "values": [{"id": "ft", "count": len(every), "descriptor": "Full time"}],
        },
    ]

    def search(url: str, body: dict[str, Any]) -> httpx.Response:
        families = body["appliedFacets"].get("jobFamilyGroup")
        pool = jobs_by_family[families[0]] if families else every
        page = pool[body["offset"] : body["offset"] + body["limit"]]
        total = (len(pool) if families else total_without_facets) if body["offset"] == 0 else 0
        return httpx.Response(200, json={
            "total": total,
            "facets": facets,
            "jobPostings": [{"externalPath": f"/job/Loc/Job_{j}", "title": j} for j in page],
        })

    return search


def listed_paths(search, **kwargs: Any) -> tuple[list[str], FakeWorkday]:
    transport = FakeWorkday(search=search)
    fetcher = WorkdayFetcher(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None), **kwargs)
    site = fetcher._remember(workday_address(BD_BOARD))
    return fetcher._list(site, {}, ""), transport


def test_pages_are_fetched_until_the_total() -> None:
    jobs = [f"R{i}" for i in range(45)]
    paths, transport = listed_paths(synthetic_board({"eng": jobs}, total_without_facets=45))
    assert paths == [f"Loc/Job_R{i}" for i in range(45)]
    offsets = sorted(body["offset"] for m, _, body in transport.requests if m == "POST")
    assert offsets == [0, 20, 40]


def test_an_over_cap_board_is_split_by_a_facet_and_merged() -> None:
    eng = [f"E{i}" for i in range(30)]
    sales = [f"S{i}" for i in range(25)] + ["E0"]  # a job in two families
    paths, transport = listed_paths(
        synthetic_board({"eng": eng, "sales": sales}, total_without_facets=RESULT_CAP)
    )
    assert len(paths) == 55
    assert len(set(paths)) == 55
    applied = [body["appliedFacets"] for m, _, body in transport.requests if m == "POST"]
    assert applied[0] == {}
    assert {"jobFamilyGroup": ["eng"]} in applied
    assert {"jobFamilyGroup": ["sales"]} in applied
    # Never paged past the cap on the unfiltered search.
    assert all(body["offset"] < RESULT_CAP for m, _, body in transport.requests if m == "POST")


def test_nvidia_would_be_split_by_job_family() -> None:
    """Real NVIDIA facets: three facets agree on 2,654 jobs; locations overlap."""
    groups = list(_facet_groups(load("nvidia_search_page0_facets.json")["facets"]))
    assert {param for param, _ in groups} >= {
        "jobFamilyGroup", "timeType", "locationHierarchy1", "locations",
    }
    assert _estimated_total(groups) == 2654
    param, values = _split_facet(groups, {})
    assert param == "jobFamilyGroup"
    assert len(values) == 14
    # A facet already applied is never split on again.
    assert _split_facet(groups, {"jobFamilyGroup": ["x"]})[0] != "jobFamilyGroup"


def test_post_retries_like_get() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503 if len(calls) == 1 else 200, json={"ok": True})

    client = HttpClient(transport=httpx.MockTransport(handler), backoff=0.0, sleep=lambda _: None)
    assert client.post("https://x.example/jobs", {"a": 1}).json() == {"ok": True}
    assert len(calls) == 2


# --------------------------------------------------------------------------
# Live tests: real network, deselected by default (`make test-live`)
# --------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.parametrize("url", [NV_JOB, BD_JOB])
def test_live_job_matches_fixture(url: str) -> None:
    """Catches API drift (or the job closing): if this fails, re-save the fixtures."""
    with FetcherRegistry() as live:
        posting = live.fetch(parse_url(url))
    saved = load(f"{posting.company_slug}_job_{posting.job_id}.json")["jobPostingInfo"]
    assert posting.title == saved["title"]
    assert posting.description_text


@pytest.mark.live
def test_live_board_search_lists_jobs() -> None:
    """Catches changes to the search endpoint: one page, no descriptions needed."""
    with FetcherRegistry() as live:
        fetcher = live.fetcher_for(Platform.WORKDAY)
        assert isinstance(fetcher, WorkdayFetcher)
        site = fetcher._remember(workday_address(BD_BOARD))
        page = fetcher._search(site, {}, "", 0)
    assert page["total"] > 0
    assert page["jobPostings"][0]["externalPath"].startswith("/job/")
    assert list(_facet_groups(page["facets"]))
