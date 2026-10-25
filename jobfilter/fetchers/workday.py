"""Workday fetcher.

Every Workday career site ({tenant}.wd{N}.myworkdayjobs.com/{site}) is a
single-page app over an unauthenticated JSON API:

    POST /wday/cxs/{tenant}/{site}/jobs        search: 20 per page, no descriptions
    GET  /wday/cxs/{tenant}/{site}/job/{path}  one posting, description as HTML

So a whole board costs one search request per 20 jobs plus one request per
job. Quirks, all seen on NVIDIA and Boston Dynamics (2026-10):

- `total` is only reported on the first page; later pages say 0.
- A search stops at 2000 results: later offsets repeat earlier jobs. A bigger
  board is split along a facet whose every value is under the cap (NVIDIA's
  2,654 jobs, by its 14 job families) and the parts are merged.
- A facet the site does not have is a 400, so a board URL's query parameters
  are only forwarded when the site lists them as facets.
- The job endpoint needs the title in the path, not just the req id.
- Hundreds of job requests in a row draw HTTP 429 (NVIDIA: 26 of 2,654 at
  6 in parallel). Jobs that fail that way are retried once more, one at a
  time, after a pause.
- A repost's path ends in the req id plus "-1". The id in the path is the job
  id, as it is in the browser extension, so both land on one stored posting.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Iterable, Sequence, TypeVar
from urllib.parse import unquote

from ..models import ParsedUrl, Platform, Posting
from ..url_parser import WorkdayAddress, workday_address
from .base import BoardNotFound, FetchError, JobNotFound, TransportFailure
from .http import HttpClient
from .textutil import html_to_text

log = logging.getLogger(__name__)

PAGE_SIZE = 20  # the API refuses anything larger
RESULT_CAP = 2000
WORKERS = 6  # parallel requests: a full NVIDIA scan in minutes, not tens of them
RETRY_PAUSE = 5.0  # seconds before re-fetching jobs that failed in the parallel pass
MAX_SPLIT_DEPTH = 2
# Search-page parameters that are not facets.
_SEARCH_PARAM = "q"

Facets = dict[str, list[str]]
T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class WorkdaySite:
    """One career site: a tenant can have several (external, university, ...)."""

    host: str
    tenant: str
    site: str

    @classmethod
    def of(cls, address: WorkdayAddress) -> WorkdaySite:
        return cls(address.host, address.tenant, address.site)

    @property
    def api(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    @property
    def web(self) -> str:
        return f"https://{self.host}/{self.site}"


class WorkdayFetcher:
    platform: ClassVar[Platform] = Platform.WORKDAY

    def __init__(self, client: HttpClient, workers: int = WORKERS) -> None:
        self._client = client
        self._workers = workers
        # Per run: the sites seen for each tenant, so `--expand` can find a
        # board from the tenant slug alone, and what was already fetched.
        self._sites: dict[str, list[WorkdaySite]] = {}
        self._listings: dict[tuple[WorkdaySite, str, str], list[str]] = {}
        self._jobs: dict[tuple[WorkdaySite, str], Posting] = {}

    # --- one job -------------------------------------------------------------

    def fetch(self, parsed: ParsedUrl) -> Posting:
        address = workday_address(parsed.url)
        assert address and address.job_path  # guaranteed by url_parser
        site = self._remember(address)
        return self._fetch_job(site, address.job_path, url=parsed.url)

    def _fetch_job(self, site: WorkdaySite, job_path: str, url: str | None = None) -> Posting:
        cached = self._jobs.get((site, job_path))
        if cached is not None:
            return cached
        api_url = f"{site.api}/job/{job_path}"
        response = self._client.get(api_url, platform=self.platform)
        if response.status_code in (404, 422):
            raise JobNotFound(
                f"Workday has no job {job_path!r} on {site.web} -- it may have "
                f"been taken down, or the site does not exist",
                url=url or f"{site.web}/job/{job_path}",
                platform=self.platform,
            )
        info = _json(response, api_url, self.platform).get("jobPostingInfo")
        if not isinstance(info, dict):
            raise JobNotFound(
                f"Workday answered without a posting for {job_path!r} on {site.web}",
                url=url or f"{site.web}/job/{job_path}",
                platform=self.platform,
            )
        posting = _to_posting(site, info, url)
        self._jobs[(site, job_path)] = posting
        return posting

    # --- whole boards --------------------------------------------------------

    def fetch_board(self, slug: str) -> list[Posting | FetchError]:
        """Every job at a tenant, on every site of it seen in this run.

        Workday boards are addressed by host and site name, which a tenant
        slug alone does not give: the site is learned from a job or board URL.
        """
        return self._fetch_jobs(self._board_paths(slug))

    def list_board(self, slug: str) -> list[ParsedUrl]:
        """Every job at a tenant as a job URL: the searches, no descriptions."""
        return [
            ParsedUrl(
                platform=self.platform,
                slug=slug,
                job_id=_path_id(unquote(path.rsplit("/", 1)[-1])),
                url=f"{site.web}/job/{path}",
            )
            for site, path in self._board_paths(slug)
        ]

    def learn(self, parsed: ParsedUrl) -> None:
        """Note the career site a job URL names. No request."""
        address = workday_address(parsed.url)
        if address is not None:
            self._remember(address)

    def _board_paths(self, slug: str) -> list[tuple[WorkdaySite, str]]:
        sites = self._sites.get(slug)
        if not sites:
            raise BoardNotFound(
                f"no Workday career site seen for {slug!r} in this run -- a "
                f"Workday board is found from one of its job or board URLs",
                url=f"workday:{slug}",
                platform=self.platform,
            )
        paths: list[tuple[WorkdaySite, str]] = []
        for site in list(sites):
            paths += [(site, path) for path in self._list(site, {}, "")]
        return paths

    def fetch_board_url(self, parsed: ParsedUrl) -> list[Posting | FetchError]:
        """Every job on the board a URL shows, with the URL's search and facets."""
        address = workday_address(parsed.url)
        assert address  # guaranteed by url_parser
        site = self._remember(address)
        search = " ".join(address.query.get(_SEARCH_PARAM, []))
        facets = {k: v for k, v in address.query.items() if k != _SEARCH_PARAM}
        return self._fetch_jobs([(site, path) for path in self._list(site, facets, search)])

    def _remember(self, address: WorkdayAddress) -> WorkdaySite:
        site = WorkdaySite.of(address)
        known = self._sites.setdefault(site.tenant, [])
        if site not in known:
            known.append(site)
        return site

    def _fetch_jobs(self, paths: Sequence[tuple[WorkdaySite, str]]) -> list[Posting | FetchError]:
        """Fetch every job. A job that fails is returned as its error, not dropped."""
        unique = list(dict.fromkeys(paths))
        log.info("workday: fetching %d job descriptions", len(unique))
        results = self._parallel(self._try_job, unique, progress="descriptions")

        # Rate limiting and flaky responses: one slower pass over what failed.
        retry = [i for i, r in enumerate(results) if isinstance(r, TransportFailure)]
        if retry:
            log.info("workday: %d failed (rate limited?); retrying one at a time", len(retry))
            self._client.pause(RETRY_PAUSE)
            for index in retry:
                results[index] = self._try_job(unique[index])
        return results

    def _try_job(self, item: tuple[WorkdaySite, str]) -> Posting | FetchError:
        site, path = item
        try:
            return self._fetch_job(site, path)
        except FetchError as exc:
            # Report the job's page, not the API call behind it.
            exc.url = f"{site.web}/job/{path}"
            return exc

    def _list(self, site: WorkdaySite, url_facets: Facets, search: str) -> list[str]:
        """Job paths on a board, cached per (site, facets, search)."""
        facets = self._known_facets(site, url_facets, search) if url_facets else {}
        key = (site, repr(sorted(facets.items())), search)
        if key not in self._listings:
            paths = self._collect(site, facets, search, depth=0)
            log.info("workday: %s lists %d jobs", site.web, len(paths))
            self._listings[key] = paths
        return self._listings[key]

    def _known_facets(self, site: WorkdaySite, url_facets: Facets, search: str) -> Facets:
        """Keep the URL parameters the site has as facets; Workday 400s on others."""
        page = self._search(site, {}, search, 0)
        known = {param for param, _ in _facet_groups(page.get("facets") or [])}
        dropped = sorted(set(url_facets) - known)
        if dropped:
            log.info("workday: ignoring URL parameters that are not facets: %s", ", ".join(dropped))
        return {k: v for k, v in url_facets.items() if k in known}

    def _collect(self, site: WorkdaySite, facets: Facets, search: str, depth: int) -> list[str]:
        first = self._search(site, facets, search, 0)
        total = int(first.get("total") or 0)
        if total < RESULT_CAP:
            offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))
            pages = [first] + self._parallel(
                lambda offset: self._search(site, facets, search, offset), offsets
            )
            return _paths(pages)

        groups = list(_facet_groups(first.get("facets") or []))
        split = _split_facet(groups, facets) if depth < MAX_SPLIT_DEPTH else None
        if split is None:
            log.warning(
                "workday: %s has %d+ matching jobs and no facet to split them by; "
                "only the first %d are checked",
                site.web, RESULT_CAP, RESULT_CAP,
            )
            offsets = list(range(PAGE_SIZE, RESULT_CAP, PAGE_SIZE))
            pages = [first] + self._parallel(
                lambda offset: self._search(site, facets, search, offset), offsets
            )
            return _paths(pages)

        param, value_ids = split
        expected = _estimated_total(groups)
        log.info(
            "workday: %s has ~%d jobs, over the %d search cap; splitting by %s (%d parts)",
            site.web, expected, RESULT_CAP, param, len(value_ids),
        )
        paths: list[str] = []
        for value_id in value_ids:
            paths += self._collect(site, {**facets, param: [value_id]}, search, depth + 1)
        paths = list(dict.fromkeys(paths))
        if len(paths) < expected:
            log.warning(
                "workday: %s: listed %d of ~%d jobs; the rest have no %s value",
                site.web, len(paths), expected, param,
            )
        return paths

    def _search(self, site: WorkdaySite, facets: Facets, search: str, offset: int) -> dict[str, Any]:
        api_url = f"{site.api}/jobs"
        response = self._client.post(
            api_url,
            {"appliedFacets": facets, "limit": PAGE_SIZE, "offset": offset, "searchText": search},
            platform=self.platform,
        )
        if response.status_code in (404, 422):
            # 404: no such site at this tenant. 422: no such tenant.
            raise BoardNotFound(
                f"no Workday career site at {site.web} (HTTP {response.status_code})",
                url=site.web,
                platform=self.platform,
            )
        return _json(response, api_url, self.platform)

    def _parallel(
        self, work: Callable[[T], R], items: Sequence[T], progress: str | None = None
    ) -> list[R]:
        """`work` over `items` on a small thread pool, results in input order."""
        if not items:
            return []
        results: list[R] = []
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            for done, result in enumerate(pool.map(work, items), start=1):
                results.append(result)
                if progress and done % 100 == 0:
                    log.info("workday: %d/%d %s", done, len(items), progress)
        return results


# --- pure helpers --------------------------------------------------------------


def _json(response: Any, url: str, platform: Platform) -> dict[str, Any]:
    if response.status_code >= 400:
        raise TransportFailure(
            f"{url} answered HTTP {response.status_code}", url=url, platform=platform
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TransportFailure(
            f"{url} returned {response.status_code} with a non-JSON body",
            url=url,
            platform=platform,
        ) from exc
    if not isinstance(payload, dict):
        raise TransportFailure(f"{url} returned unexpected JSON", url=url, platform=platform)
    return payload


def _paths(pages: Iterable[dict[str, Any]]) -> list[str]:
    """`/job/US-CA-Santa-Clara/Title_JR1` -> `US-CA-Santa-Clara/Title_JR1`, deduplicated."""
    paths: list[str] = []
    for page in pages:
        for job in page.get("jobPostings") or []:
            path = (job.get("externalPath") or "").removeprefix("/job/")
            if path:
                paths.append(path)
    return list(dict.fromkeys(paths))


FacetGroup = tuple[str, list[dict[str, Any]]]


def _facet_groups(facets: Iterable[dict[str, Any]]) -> Iterable[FacetGroup]:
    """(parameter, values) for every facet, including those nested in a group.

    NVIDIA's "locationMainGroup" holds three real facets (location type,
    country, site), each with its own parameter.
    """
    for facet in facets:
        values = facet.get("values") or []
        if values and all("values" in value for value in values):
            yield from _facet_groups(values)
        elif facet.get("facetParameter"):
            yield facet["facetParameter"], values


def _counted(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [v for v in values if isinstance(v.get("count"), int) and v.get("id")]


def _estimated_total(groups: Sequence[FacetGroup]) -> int:
    """How many jobs match, when `total` stops at the cap.

    A facet every job has exactly one value of (job family, time type) sums
    to the true total, and such facets agree with each other: NVIDIA's three
    all sum to 2,654. A job can be in several locations, so location facets
    sum higher; a facet some jobs lack sums lower. So: the largest sum that
    two facets agree on, else the largest sum.
    """
    sums = [sum(v["count"] for v in _counted(values)) for _, values in groups]
    agreed = [total for total in sums if sums.count(total) > 1]
    return max(agreed or sums or [0])


def _split_facet(groups: Sequence[FacetGroup], applied: Facets) -> tuple[str, list[str]] | None:
    """The facet to split an over-cap search by, and its value ids.

    Every value must be under the cap, so each part can be listed in full,
    and the values must cover every job (a job with no value for the facet
    would be missed). Among those, the fewest parts. If no facet covers
    everything, the one covering the most.
    """
    expected = _estimated_total(groups)
    # (misses some jobs, parts if it covers all else -coverage, param, ids)
    best: tuple[bool, int, str, list[str]] | None = None
    for param, values in groups:
        counted = _counted(values)
        if param in applied or len(counted) < 2:
            continue
        if any(v["count"] >= RESULT_CAP for v in counted):
            continue
        covered = sum(v["count"] for v in counted)
        misses = covered < expected
        candidate = (misses, -covered if misses else len(counted), param, [v["id"] for v in counted])
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return (best[2], best[3]) if best else None


def _path_id(job_posting_id: str | None) -> str | None:
    """"Senior-Field-Engineer_JR2021006-1" -> "JR2021006-1"."""
    if not job_posting_id or "_" not in job_posting_id:
        return None
    return job_posting_id.rsplit("_", 1)[1] or None


def _to_posting(site: WorkdaySite, info: dict[str, Any], url: str | None) -> Posting:
    job_posting_id = info.get("jobPostingId") or ""
    locations = [info.get("location"), *(info.get("additionalLocations") or [])]
    return Posting(
        platform=Platform.WORKDAY,
        company_slug=site.tenant,
        job_id=_path_id(job_posting_id) or str(info.get("jobReqId") or info.get("id") or ""),
        title=info.get("title") or "",
        location="; ".join(loc for loc in locations if loc) or None,
        url=url or info.get("externalUrl") or f"{site.web}/job/{job_posting_id}",
        description_text=html_to_text(info.get("jobDescription") or ""),
        raw=info,
        posted_ago=info.get("postedOn") or None,
    )
