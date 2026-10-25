"""Eightfold fetcher.

Eightfold AI runs career sites on the employer's own domain
(careers.qualcomm.com, apply.careers.microsoft.com, explore.jobs.netflix.net),
each over the same unauthenticated JSON API on that host:

    GET /careers                                            the site: names its domain, sets a cookie
    GET /api/pcsx/position_details?position_id={id}&domain={domain}   one job, description as HTML
    GET /api/pcsx/search?domain={domain}&query=&location=&start={n}    search: 10 per page, no text
    GET /api/apply/v2/jobs?domain={domain}&...&start={n}&num=10        the same, on older sites

Quirks, all seen on Qualcomm, Microsoft and Netflix (2026-10):

- Every call needs the site's `domain` ("qualcomm.com"), which its page names
  in `window._EF_GROUP_ID`. So each host's page is read once per run, first.
  That also sets the session cookie without which Netflix answers "Please
  try again later" (as HTTP 200, not JSON).
- Sites come in two generations. Newer ones (Qualcomm, Microsoft) search
  through /api/pcsx/search and refuse /api/apply/v2/jobs ("Not authorized for
  PCSX"); older ones (Netflix) the other way round ("PCSX is not enabled").
  A search tries the newer API first and remembers which one answered.
  position_details answers on both.
- A search page holds 10 jobs whatever `num` asks for, and no descriptions:
  a whole board costs one request per 10 jobs plus one per job. Qualcomm
  lists ~2,100 jobs and Microsoft ~2,400, with no cap on how deep a search goes.
- `count` drifts between pages (Microsoft: 2,440, then 2,433), so a short or
  empty page near the end is expected.
- Qualcomm's site sits behind an AWS firewall. A board scan at full speed
  (~770 requests in under a minute) drew HTTP 405 and a "Human Verification"
  captcha page for every request after, for several minutes. So requests to a
  host are spaced out across all threads (refresh fetches from its own pool),
  and a 405 is not a failed job but a signal: every request to that host waits,
  then goes on at half the pace. A host still refusing after
  MAX_BLOCKED_TRIES waits is given up for the run with RateLimited, and
  nothing more is sent to it.

A company's slug is its host's name ("qualcomm"), read by url_parser. A
board is found from one of the company's job or search URLs, as on Workday.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, ClassVar, Sequence, TypeVar
from urllib.parse import urlencode

import httpx

from ..models import ParsedUrl, Platform, Posting
from ..url_parser import EightfoldAddress, eightfold_address, eightfold_slug
from .base import BoardNotFound, FetchError, JobNotFound, RateLimited, TransportFailure
from .http import HttpClient
from .textutil import html_to_text

log = logging.getLogger(__name__)

PAGE_SIZE = 10  # the search's fixed page size
WORKERS = 3  # enough to keep the pace below busy; the pace, not this, sets the speed
RETRY_PAUSE = 5.0  # seconds before re-fetching jobs that failed in the parallel pass
INTERVAL = 0.5  # seconds between requests to one host, to start with
MAX_INTERVAL = 8.0
COOLDOWN = 60.0  # seconds every request to a host waits once its firewall refuses one
MAX_BLOCKED_TRIES = 15  # a request refused this many times gives the host up (~15 minutes)
_GROUP_ID_RE = re.compile(r"""_EF_GROUP_ID\s*=\s*["']([^"']+)["']""")
_SHARED_HOST_SUFFIX = ".eightfold.ai"

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class EightfoldSite:
    """One career site: the host it is served from and the domain its API takes."""

    host: str
    domain: str

    @property
    def web(self) -> str:
        return f"https://{self.host}"

    @property
    def slug(self) -> str:
        return eightfold_slug(self.host, self.domain) or self.domain

    def api(self, path: str, **params: str | int) -> str:
        return f"{self.web}/api/{path}?{urlencode({'domain': self.domain, **params})}"

    def job_url(self, job_id: str) -> str:
        url = f"{self.web}/careers/job/{job_id}"
        # Eightfold's own hosts serve many employers: the domain says which.
        return f"{url}?domain={self.domain}" if self.host.endswith(_SHARED_HOST_SUFFIX) else url


class HostPace:
    """Spaces out requests to one host across threads, and slows down when refused."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.given_up = False
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Reserve the next turn to send; how many seconds until it comes."""
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.interval
            return at - now

    def refused(self, cooldown: float) -> bool:
        """Hold every request for `cooldown`, then halve the pace.

        True if this refusal started the hold. Requests already in flight are
        refused too; they join the hold rather than doubling the pace again.
        """
        with self._lock:
            now = time.monotonic()
            if self._next - now > cooldown / 2:
                return False
            self.interval = min(max(self.interval * 2, 1.0), MAX_INTERVAL)
            self._next = now + cooldown
            return True


@dataclass(frozen=True, slots=True)
class SearchPage:
    count: int
    job_ids: list[str]


class EightfoldFetcher:
    platform: ClassVar[Platform] = Platform.EIGHTFOLD

    def __init__(
        self, client: HttpClient, workers: int = WORKERS, interval: float = INTERVAL
    ) -> None:
        self._client = client
        self._workers = workers
        self._interval = interval
        self._paces: dict[str, HostPace] = {}
        self._paces_lock = threading.Lock()
        # Per run: the hosts seen for each company, each site once its page is
        # read, which search API each host answers, and what was fetched.
        self._seen: dict[str, dict[str, EightfoldAddress]] = {}
        self._sites: dict[str, EightfoldSite] = {}
        self._search_api: dict[str, str] = {}
        self._listings: dict[tuple[str, str], list[str]] = {}
        self._jobs: dict[tuple[str, str], Posting] = {}

    # --- one job -------------------------------------------------------------

    def fetch(self, parsed: ParsedUrl) -> Posting:
        address = eightfold_address(parsed.url)
        assert address and address.job_id  # guaranteed by url_parser
        self._note(address)
        return self._fetch_job(self._site(address), address.job_id, url=parsed.url)

    def _fetch_job(self, site: EightfoldSite, job_id: str, url: str | None = None) -> Posting:
        cached = self._jobs.get((site.host, job_id))
        if cached is not None:
            return cached
        api_url = site.api("pcsx/position_details", position_id=job_id, hl="en")
        found = self._get_json(site.host, api_url)
        if found is None:  # HTTP 404, "Position not found"
            raise JobNotFound(
                f"{site.host} has no job {job_id!r} -- it may have been filled or taken down",
                url=url or site.job_url(job_id),
                platform=self.platform,
            )
        job = found.get("data") if isinstance(found, dict) else None
        if not isinstance(job, dict) or not job.get("id"):
            raise TransportFailure(
                f"{api_url} answered without a job: {_message(found)}",
                url=url or site.job_url(job_id),
                platform=self.platform,
            )
        posting = _to_posting(site, job)
        self._jobs[(site.host, job_id)] = posting
        return posting

    # --- whole boards --------------------------------------------------------

    def fetch_board(self, slug: str) -> list[Posting | FetchError]:
        """Every job at a company, on every site of it seen in this run."""
        return self._fetch_jobs(self._board_ids(slug))

    def list_board(self, slug: str) -> list[ParsedUrl]:
        """Every job at a company as a job URL: the searches, no descriptions."""
        return [
            ParsedUrl(platform=self.platform, slug=slug, job_id=job_id, url=site.job_url(job_id))
            for site, job_id in self._board_ids(slug)
        ]

    def fetch_board_url(self, parsed: ParsedUrl) -> list[Posting | FetchError]:
        """Every job a search URL finds, with its query, location and filters."""
        address = eightfold_address(parsed.url)
        assert address  # guaranteed by url_parser
        self._note(address)
        site = self._site(address)
        return self._fetch_jobs([(site, job_id) for job_id in self._list(site, address.search)])

    def learn(self, parsed: ParsedUrl) -> None:
        """Note the site a job URL names. No request."""
        address = eightfold_address(parsed.url)
        if address is not None:
            self._note(address)

    def _note(self, address: EightfoldAddress) -> None:
        self._seen.setdefault(address.slug, {}).setdefault(address.host, address)

    def _board_ids(self, slug: str) -> list[tuple[EightfoldSite, str]]:
        addresses = self._seen.get(slug)
        if not addresses:
            raise BoardNotFound(
                f"no Eightfold career site seen for {slug!r} in this run -- an "
                f"Eightfold board is found from one of its job or search URLs",
                url=f"eightfold:{slug}",
                platform=self.platform,
            )
        ids: list[tuple[EightfoldSite, str]] = []
        for address in list(addresses.values()):
            site = self._site(address)
            ids += [(site, job_id) for job_id in self._list(site, {})]
        return ids

    def _site(self, address: EightfoldAddress) -> EightfoldSite:
        """The site at a host: its page read once, for the domain and the cookie."""
        cached = self._sites.get(address.host)
        if cached is not None:
            return cached
        page_url = f"https://{address.host}/careers"
        if address.domain:
            page_url += f"?domain={address.domain}"
        response = self._get(address.host, page_url)
        if response.status_code == 404:
            raise BoardNotFound(
                f"{address.host} has no Eightfold career site (HTTP 404)",
                url=page_url,
                platform=self.platform,
            )
        if response.status_code >= 400:
            raise TransportFailure(
                f"{page_url} answered HTTP {response.status_code}", url=page_url, platform=self.platform
            )
        match = _GROUP_ID_RE.search(response.text)
        domain = match.group(1).strip().lower() if match else address.domain
        if not domain:
            raise BoardNotFound(
                f"{address.host} does not look like an Eightfold career site: its "
                f"/careers page names no domain",
                url=page_url,
                platform=self.platform,
            )
        site = EightfoldSite(address.host, domain)
        self._sites[address.host] = site
        return site

    def _list(self, site: EightfoldSite, search: dict[str, str]) -> list[str]:
        """Job ids a search finds, cached per (host, search)."""
        key = (site.host, repr(sorted(search.items())))
        cached = self._listings.get(key)
        if cached is not None:
            return cached
        first = self._search(site, search, 0)
        rest = self._parallel(
            lambda start: self._search(site, search, start),
            list(range(PAGE_SIZE, first.count, PAGE_SIZE)),
        )
        job_ids = list(dict.fromkeys(job_id for page in [first, *rest] for job_id in page.job_ids))
        if not job_ids:
            # A board that lists nothing must not read as "every job was taken down".
            wanted = ", ".join(f"{k}={v!r}" for k, v in search.items()) or "any job"
            raise BoardNotFound(
                f"{site.host} lists no jobs for {wanted}",
                url=f"{site.web}/careers",
                platform=self.platform,
            )
        log.info("eightfold: %s lists %d jobs", site.host, len(job_ids))
        self._listings[key] = job_ids
        return job_ids

    def _search(self, site: EightfoldSite, search: dict[str, str], start: int) -> SearchPage:
        """One page of a search, through whichever API this site answers."""
        api = self._search_api.get(site.host)
        answers: list[Any] = []
        if api != "v2":
            answers.append(self._get_json(site.host, site.api("pcsx/search", **search, start=start)))
            page = _search_page(answers[-1])
            if page is not None:
                self._search_api[site.host] = "pcsx"
                return page
        if api != "pcsx":
            answers.append(self._get_json(
                site.host, site.api("apply/v2/jobs", **search, start=start, num=PAGE_SIZE)
            ))
            page = _search_page(answers[-1])
            if page is not None:
                self._search_api[site.host] = "v2"
                return page
        if all(answer is None for answer in answers):
            raise BoardNotFound(
                f"{site.host} has no Eightfold job search (HTTP 404)",
                url=f"{site.web}/careers",
                platform=self.platform,
            )
        raise TransportFailure(
            f"{site.host} refused the job search: "
            + "; ".join(_message(answer) for answer in answers),
            url=f"{site.web}/careers",
            platform=self.platform,
        )

    def _fetch_jobs(self, ids: Sequence[tuple[EightfoldSite, str]]) -> list[Posting | FetchError]:
        """Fetch every job. A job that fails is returned as its error, not dropped."""
        unique = list(dict.fromkeys(ids))
        log.info("eightfold: fetching %d job descriptions", len(unique))
        results = self._parallel(self._try_job, unique)

        # Rate limiting and flaky answers: one slower pass over what failed.
        retry = [
            i for i, r in enumerate(results)
            if isinstance(r, TransportFailure) and not isinstance(r, RateLimited)
        ]
        if retry:
            log.info("eightfold: %d failed (rate limited?); retrying one at a time", len(retry))
            self._client.pause(RETRY_PAUSE)
            for index in retry:
                results[index] = self._try_job(unique[index])
        return results

    def _try_job(self, item: tuple[EightfoldSite, str]) -> Posting | FetchError:
        site, job_id = item
        try:
            return self._fetch_job(site, job_id)
        except FetchError as exc:
            # Report the job's page, not the API call behind it.
            exc.url = site.job_url(job_id)
            return exc

    # --- requests ------------------------------------------------------------

    def _pace(self, host: str) -> HostPace:
        with self._paces_lock:
            return self._paces.setdefault(host, HostPace(self._interval))

    def _get(self, host: str, url: str) -> httpx.Response:
        """GET at the host's pace, waiting out its firewall when it refuses."""
        pace = self._pace(host)
        for _ in range(MAX_BLOCKED_TRIES):
            if pace.given_up:
                break
            self._client.pause(pace.wait())
            try:
                response = self._client.get(url, platform=self.platform)
            except TransportFailure as exc:
                # The client retries a 429 once itself, then gives up.
                cause = getattr(exc.__cause__, "response", None)
                if cause is None or not _refused(cause):
                    raise
                response = cause
            if not _refused(response):
                return response
            if pace.refused(COOLDOWN):
                log.warning(
                    "eightfold: %s is refusing requests (HTTP %d, rate limit); waiting %ds, "
                    "then one request every %.1fs",
                    host, response.status_code, COOLDOWN, pace.interval,
                )
        pace.given_up = True
        raise RateLimited(
            f"{host} kept refusing requests for asking too often (its firewall asks for a "
            f"captcha); nothing more was sent to it this run -- try again in a while",
            url=url,
            platform=self.platform,
        )

    def _get_json(self, host: str, url: str) -> Any:
        """GET and decode JSON; None for a 404. A body that is not JSON is a transport failure."""
        response = self._get(host, url)
        if response.status_code == 404:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise TransportFailure(
                f"GET {url} returned {response.status_code} with a non-JSON body",
                url=url,
                platform=self.platform,
            ) from exc

    def _parallel(self, work: Callable[[T], R], items: Sequence[T]) -> list[R]:
        """`work` over `items` on a small thread pool, results in input order."""
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            return list(pool.map(work, items))


# --- pure helpers --------------------------------------------------------------


def _refused(response: httpx.Response) -> bool:
    """The firewall's answer to too many requests: 405 with a captcha page, or 429."""
    return response.status_code in (405, 429) or "x-amzn-waf-action" in response.headers


def _search_page(answer: Any) -> SearchPage | None:
    """A page of either search API; None if the answer is a refusal.

    The newer API wraps its page in `data`, the older one does not.
    """
    if not isinstance(answer, dict):
        return None
    body = answer.get("data") if isinstance(answer.get("data"), dict) else answer
    positions = body.get("positions")
    if not isinstance(positions, list):
        return None
    job_ids = [str(p["id"]) for p in positions if isinstance(p, dict) and p.get("id")]
    return SearchPage(count=int(body.get("count") or 0), job_ids=job_ids)


def _message(answer: Any) -> str:
    """What a refusal says: {"message": "Not authorized for PCSX"} and its kin."""
    if isinstance(answer, dict):
        error = answer.get("error")
        for text in (answer.get("message"), error.get("message") if isinstance(error, dict) else None):
            if isinstance(text, str) and text.strip():
                return text.strip()
        if answer.get("messages"):
            return str(answer["messages"])
    return "no job search here" if answer is None else "an unexpected answer"


def _posted(job: dict[str, Any]) -> str | None:
    """"Posted 2026-09-17", from the posting's Unix time."""
    ts = job.get("postedTs") or job.get("creationTs")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return f"Posted {datetime.fromtimestamp(ts, UTC).date().isoformat()}"


def _to_posting(site: EightfoldSite, job: dict[str, Any]) -> Posting:
    job_id = str(job["id"])
    # As the job lists them ("San Diego, California, United States of America"):
    # `standardizedLocations` files a remote US job under Oregon.
    locations = [loc for loc in job.get("locations") or [] if isinstance(loc, str) and loc.strip()]
    return Posting(
        platform=Platform.EIGHTFOLD,
        company_slug=site.slug,
        job_id=job_id,
        title=(job.get("name") or "").strip(),
        location="; ".join(dict.fromkeys(locations)) or job.get("location") or None,
        url=site.job_url(job_id),
        description_text=html_to_text(job.get("jobDescription") or ""),
        raw=job,
        posted_ago=_posted(job),
    )
