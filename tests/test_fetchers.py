"""Fetcher tests. Offline by default against tests/fixtures/."""

from __future__ import annotations

import httpx
import pytest

from jobfilter.fetchers import (
    BoardNotFound,
    FetcherRegistry,
    HttpClient,
    JobNotFound,
    TransportFailure,
    UnsupportedPlatform,
)
from jobfilter.models import Platform, Posting
from jobfilter.url_parser import parse_url

from .conftest import (
    ASHBY_JOB_ID,
    ASHBY_SLUG,
    ASHBY_URL,
    GH_JOB_ID,
    GH_SLUG,
    GH_URL,
    LEVER_JOB_ID,
    LEVER_SLUG,
    LEVER_URL,
    RecordingTransport,
    ashby_sibling_job_id,
)


def fetch(registry: FetcherRegistry, url: str) -> Posting:
    return registry.fetch(parse_url(url))


# --------------------------------------------------------------------------
# Normalization: each platform lands in the same Posting shape
# --------------------------------------------------------------------------

def test_ashby_posting_is_normalized(registry: FetcherRegistry) -> None:
    posting = fetch(registry, ASHBY_URL)
    assert posting.platform is Platform.ASHBY
    assert posting.company_slug == ASHBY_SLUG
    assert posting.job_id == ASHBY_JOB_ID
    assert posting.title == "AI Research Intern: Foundation Models"
    assert posting.location == "Los Angeles - HQ"
    assert posting.url == ASHBY_URL
    assert posting.raw["id"] == ASHBY_JOB_ID


def test_greenhouse_posting_is_normalized(registry: FetcherRegistry) -> None:
    posting = fetch(registry, GH_URL)
    assert posting.platform is Platform.GREENHOUSE
    assert posting.company_slug == GH_SLUG
    assert posting.job_id == GH_JOB_ID
    assert posting.title == "Robotics Software Engineer"
    assert posting.location == "Sunnyvale, CA"
    # Normalization drops company_name; raw keeps it for later phases.
    assert posting.raw["company_name"] == "Noble Machines, Inc"


def test_lever_posting_is_normalized(registry: FetcherRegistry) -> None:
    posting = fetch(registry, LEVER_URL)
    assert posting.platform is Platform.LEVER
    assert posting.company_slug == LEVER_SLUG
    assert posting.job_id == LEVER_JOB_ID
    assert posting.title == "Autonomous Agent Engineering Intern"
    assert posting.location == "Zurich"


@pytest.mark.parametrize("url", [ASHBY_URL, GH_URL, LEVER_URL])
def test_description_is_plain_text(registry: FetcherRegistry, url: str) -> None:
    """No markup and no leftover entities reach the classifier."""
    text = fetch(registry, url).description_text
    assert text
    for residue in ("<p>", "<li>", "<div>", "<br", "&lt;", "&gt;", "&amp;", "&nbsp;"):
        assert residue not in text, f"{residue!r} survived in {url}"


# --------------------------------------------------------------------------
# Platform-specific content handling
# --------------------------------------------------------------------------

def test_ashby_keeps_the_export_control_section(registry: FetcherRegistry) -> None:
    """Step 3's ground truth has to survive fetching intact."""
    text = fetch(registry, ASHBY_URL).description_text
    assert "Export Control Requirements" in text
    assert "U.S. citizen or national" in text
    assert "lawful permanent resident" in text
    assert "Refugee under 8 U.S.C." in text
    assert "Asylee under 8 U.S.C." in text


def test_greenhouse_unescapes_double_escaped_html(registry: FetcherRegistry) -> None:
    """`content` arrives as `&lt;p&gt;`; one unescape then strip tags."""
    raw = fetch(registry, GH_URL).raw["content"]
    assert raw.startswith("&lt;p&gt;"), "fixture should still be double-escaped"

    text = fetch(registry, GH_URL).description_text
    assert "Robotics Software Engineer" in text
    assert "Noble Machines builds multipurpose rob" in text
    assert "California Pay Transparency" in text


def test_lever_combines_description_lists_and_additional(
    registry: FetcherRegistry,
) -> None:
    """descriptionPlain alone is 194 chars; the substance is in `lists`."""
    posting = fetch(registry, LEVER_URL)
    text = posting.description_text

    assert "Our Mission" in text                      # descriptionPlain
    assert "What You'll Do" in text                   # lists[].text heading
    assert "What You'll need" in text
    assert "Areas of Special Interest" in text
    assert "agentic framework" in text                # lists[].content body
    assert "equal employment opportunities" in text   # additionalPlain

    assert len(text) > 5 * len(posting.raw["descriptionPlain"])


def test_lever_additional_block_is_eeo_boilerplate(registry: FetcherRegistry) -> None:
    """Step 3 must NOT treat these phrases as a restriction signal."""
    text = fetch(registry, LEVER_URL).description_text
    assert "national origin" in text
    assert "protected veteran status" in text


# --------------------------------------------------------------------------
# Caching: one board request per Ashby slug per run
# --------------------------------------------------------------------------

def test_two_jobs_from_one_company_cost_one_request(
    registry: FetcherRegistry, transport: RecordingTransport
) -> None:
    sibling = ashby_sibling_job_id()

    first = fetch(registry, ASHBY_URL)
    second = fetch(registry, f"https://jobs.ashbyhq.com/{ASHBY_SLUG}/{sibling}")

    assert first.job_id == ASHBY_JOB_ID
    assert second.job_id == sibling
    assert second.title and second.title != first.title
    assert len(transport.requests) == 1, transport.requests


def test_ashby_cache_is_cold_on_a_fresh_registry(
    registry: FetcherRegistry, transport: RecordingTransport
) -> None:
    fetch(registry, ASHBY_URL)
    fetch(registry, ASHBY_URL)
    fetch(registry, ASHBY_URL)
    assert len(transport.requests) == 1, transport.requests


def test_cache_does_not_leak_between_registries(
    client: HttpClient, transport: RecordingTransport
) -> None:
    """No global state: a second run re-fetches."""
    fetch(FetcherRegistry(client), ASHBY_URL)
    fetch(FetcherRegistry(client), ASHBY_URL)
    assert len(transport.requests) == 2


def test_greenhouse_and_lever_are_not_cached(
    registry: FetcherRegistry, transport: RecordingTransport
) -> None:
    """Only Ashby needs a cache; the others have per-job endpoints."""
    fetch(registry, GH_URL)
    fetch(registry, GH_URL)
    fetch(registry, LEVER_URL)
    assert len(transport.requests) == 3


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

def test_ashby_unknown_board_raises_board_not_found(
    registry: FetcherRegistry,
) -> None:
    with pytest.raises(BoardNotFound) as exc:
        fetch(registry, "https://jobs.ashbyhq.com/no-such-company/abc-123")
    assert "no-such-company" in str(exc.value)
    assert exc.value.platform is Platform.ASHBY


def test_ashby_unknown_job_on_a_real_board_raises_job_not_found(
    registry: FetcherRegistry,
) -> None:
    url = f"https://jobs.ashbyhq.com/{ASHBY_SLUG}/00000000-0000-0000-0000-000000000000"
    with pytest.raises(JobNotFound) as exc:
        fetch(registry, url)
    message = str(exc.value)
    assert "00000000-0000-0000-0000-000000000000" in message
    assert "16 job(s) listed" in message, "error should say the board was found"


def test_greenhouse_404_raises_job_not_found(registry: FetcherRegistry) -> None:
    with pytest.raises(JobNotFound) as exc:
        fetch(registry, f"https://boards.greenhouse.io/{GH_SLUG}/jobs/1")
    assert "the job or the board does not exist" in str(exc.value)


def test_greenhouse_gh_jid_url_resolves_slug_from_job_id(
    registry: FetcherRegistry, transport
) -> None:
    url = f"https://careers.example.com/openings?gh_jid={GH_JOB_ID}"
    posting = fetch(registry, url)
    assert posting.company_slug == GH_SLUG
    assert posting.title == "Robotics Software Engineer"
    assert posting.url == url, "keep the URL the user gave us"
    assert transport.requests == [
        f"https://boards.greenhouse.io/embed/job_app?token={GH_JOB_ID}",
        f"https://boards-api.greenhouse.io/v1/boards/{GH_SLUG}/jobs/{GH_JOB_ID}",
    ]


def test_greenhouse_gh_jid_for_unknown_job_raises_job_not_found(
    registry: FetcherRegistry,
) -> None:
    with pytest.raises(JobNotFound) as exc:
        fetch(registry, "https://careers.example.com/openings?gh_jid=1")
    assert "no job '1'" in str(exc.value)
    assert exc.value.url == "https://careers.example.com/openings?gh_jid=1"


class PageTransport(RecordingTransport):
    """RecordingTransport plus HTML company pages, by URL."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        super().__init__()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        page = self.pages.get(str(request.url))
        if page is not None:
            self.requests.append(str(request.url))
            return httpx.Response(200, text=page)
        return super()._handle(request)


def page_registry(pages: dict[str, str]) -> tuple[FetcherRegistry, PageTransport]:
    transport = PageTransport(pages)
    return (
        FetcherRegistry(HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None)),
        transport,
    )


def test_ashby_jid_url_finds_the_board_named_on_the_page() -> None:
    """The real Corvus Robotics page loads jobs.ashbyhq.com/corvus-robotics/embed."""
    url = f"https://www.acme-careers.example/careers?ashby_jid={ASHBY_JOB_ID}#open-positions"
    page = (
        '<html><body><div id="ashby_embed"></div>'
        f'<script src="https://jobs.ashbyhq.com/{ASHBY_SLUG}/embed?version=2"></script>'
        "</body></html>"
    )
    registry, transport = page_registry({url.split("#")[0]: page})
    posting = fetch(registry, url)
    assert posting.company_slug == ASHBY_SLUG
    assert posting.job_id == ASHBY_JOB_ID
    assert posting.url == url, "keep the URL the user gave us"
    assert transport.requests == [
        url.split("#")[0],
        f"https://api.ashbyhq.com/posting-api/job-board/{ASHBY_SLUG}",
    ]


def test_ashby_jid_url_falls_back_to_the_domain(registry: FetcherRegistry, transport) -> None:
    """The page names no board (here: 404s); the domain is the company."""
    url = f"https://careers.{ASHBY_SLUG}.com/open-roles?ashby_jid={ASHBY_JOB_ID.upper()}"
    posting = fetch(registry, url)
    assert posting.company_slug == ASHBY_SLUG
    assert posting.job_id == ASHBY_JOB_ID
    # "careers" is skipped as a guess; the board fetched once is reused.
    assert transport.requests[-1] == f"https://api.ashbyhq.com/posting-api/job-board/{ASHBY_SLUG}"
    assert not any("/job-board/careers" in r for r in transport.requests)


def test_ashby_jid_on_a_board_that_does_not_list_it_is_not_accepted() -> None:
    """A slug on the page is only taken once its board lists the job."""
    url = "https://www.othercorp.example/careers?ashby_jid=00000000-0000-4000-8000-000000000000"
    page = f'<script src="https://jobs.ashbyhq.com/{ASHBY_SLUG}/embed"></script>'
    registry, _ = page_registry({url: page})
    with pytest.raises(JobNotFound) as exc:
        fetch(registry, url)
    message = str(exc.value)
    assert f"tried {ASHBY_SLUG}, othercorp" in message
    assert exc.value.url == url


def test_lever_404_raises_job_not_found(registry: FetcherRegistry) -> None:
    with pytest.raises(JobNotFound) as exc:
        fetch(registry, f"https://jobs.lever.co/{LEVER_SLUG}/nope")
    assert "nope" in str(exc.value)


def test_unsupported_url_raises_rather_than_being_dropped(
    registry: FetcherRegistry,
) -> None:
    with pytest.raises(UnsupportedPlatform) as exc:
        fetch(registry, "https://www.linkedin.com/jobs/view/123")
    assert exc.value.url == "https://www.linkedin.com/jobs/view/123"


def test_fetch_error_carries_url_and_platform(registry: FetcherRegistry) -> None:
    with pytest.raises(JobNotFound) as exc:
        fetch(registry, f"https://jobs.lever.co/{LEVER_SLUG}/nope")
    assert exc.value.url == f"https://jobs.lever.co/{LEVER_SLUG}/nope"
    assert exc.value.platform is Platform.LEVER


# --------------------------------------------------------------------------
# Timeout and retry policy
# --------------------------------------------------------------------------

class FlakyTransport(httpx.MockTransport):
    """Fails the first `failures` requests, then serves `response`."""

    def __init__(self, failures: int, response: httpx.Response | None = None) -> None:
        self.attempts = 0
        self._failures = failures
        self._response = response or httpx.Response(200, json={"jobs": []})
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise httpx.ConnectError("boom", request=request)
        return self._response


def _client(transport: httpx.BaseTransport) -> HttpClient:
    return HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None)


def test_one_transport_failure_is_retried_and_succeeds() -> None:
    transport = FlakyTransport(failures=1)
    _client(transport).get_json("https://api.ashbyhq.com/x")
    assert transport.attempts == 2


def test_retry_is_exhausted_after_two_attempts() -> None:
    transport = FlakyTransport(failures=99)
    with pytest.raises(TransportFailure) as exc:
        _client(transport).get_json("https://api.ashbyhq.com/x")
    assert transport.attempts == 2, "one attempt plus exactly one retry"
    assert "after 2 attempt(s)" in str(exc.value)


def test_timeout_is_retried_then_reported() -> None:
    class TimingOut(httpx.MockTransport):
        def __init__(self) -> None:
            self.attempts = 0
            super().__init__(self._handle)

        def _handle(self, request: httpx.Request) -> httpx.Response:
            self.attempts += 1
            raise httpx.ReadTimeout("slow", request=request)

    transport = TimingOut()
    with pytest.raises(TransportFailure):
        _client(transport).get_json("https://api.lever.co/x")
    assert transport.attempts == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_status_codes_are_retried(status: int) -> None:
    attempts = []

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(status)
        return httpx.Response(status)

    with pytest.raises(TransportFailure):
        _client(httpx.MockTransport(handle)).get_json("https://api.lever.co/x")
    assert len(attempts) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(status: int) -> None:
    """A 4xx will not improve on a second try; 404 becomes a typed error above."""
    attempts = []

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(status)
        return httpx.Response(status, json={})

    _client(httpx.MockTransport(handle)).get_json("https://api.lever.co/x")
    assert len(attempts) == 1


def test_backoff_grows_between_attempts() -> None:
    slept: list[float] = []
    client = HttpClient(
        transport=FlakyTransport(failures=99),
        retries=3,
        backoff=0.5,
        sleep=slept.append,
    )
    with pytest.raises(TransportFailure):
        client.get_json("https://api.lever.co/x")
    assert slept == [0.5, 1.0, 2.0]


def test_non_json_body_is_a_transport_failure() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>maintenance</html>")
    )
    with pytest.raises(TransportFailure) as exc:
        _client(transport).get_json("https://api.lever.co/x")
    assert "non-JSON body" in str(exc.value)


def test_default_timeout_is_set() -> None:
    client = HttpClient(timeout=3.5, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert client._client.timeout.read == 3.5


# --------------------------------------------------------------------------
# Live tests: real network, deselected by default (`make test-live`)
# --------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.parametrize(
    "url,expect_title",
    [
        (ASHBY_URL, "AI Research Intern: Foundation Models"),
        (GH_URL, "Robotics Software Engineer"),
        (LEVER_URL, "Autonomous Agent Engineering Intern"),
    ],
)
def test_live_fetch_matches_fixture(url: str, expect_title: str) -> None:
    """Catches API drift: if this fails, re-save the fixtures."""
    with FetcherRegistry() as live:
        posting = fetch(live, url)
    assert posting.title == expect_title
    assert posting.description_text


@pytest.mark.live
def test_live_greenhouse_gh_jid_slug_lookup() -> None:
    """Catches Greenhouse retiring the legacy embed redirect we use for slugs."""
    with FetcherRegistry() as live:
        posting = fetch(live, f"https://careers.example.com/x?gh_jid={GH_JOB_ID}")
    assert posting.company_slug == GH_SLUG


@pytest.mark.live
def test_live_ashby_jid_on_a_company_site() -> None:
    """Catches Corvus dropping the embed or the job closing (then pick another)."""
    with FetcherRegistry() as live:
        posting = fetch(
            live,
            "https://www.corvus-robotics.com/careers?ashby_jid=52fbad75-35a9-4f22-a1bc-13c8d5ac633a",
        )
    assert posting.company_slug == "corvus-robotics"
    assert posting.description_text


@pytest.mark.live
def test_live_ashby_still_has_export_control_language() -> None:
    with FetcherRegistry() as live:
        posting = fetch(live, ASHBY_URL)
    assert "Export Control Requirements" in posting.description_text
