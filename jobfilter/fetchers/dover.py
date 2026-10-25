"""Dover fetcher.

Dover's apply page (app.dover.com/apply/{Company Name}/{job_id}) is a
single-page app over unauthenticated JSON:

    GET /api/v1/inbound/application-portal-job/{job_id}   one job, description as HTML
    GET /api/v1/careers-page/{client_id}/jobs              the company's jobs, no descriptions

The company name in the URL is display text, not an id. The job response
carries the company's `client_id`, which is what `--expand` lists by, so a
company can be expanded once one of its jobs has been fetched in the run.
`visa_support` is a structured flag; it is kept in `raw` and not turned into
classifier text, since the classifier quotes sentences the posting contains.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote

from ..models import ParsedUrl, Platform, Posting
from ..url_parser import slugify_company
from .base import BoardNotFound, FetchError, JobNotFound, TransportFailure
from .http import HttpClient
from .textutil import html_to_text

API = "https://app.dover.com/api/v1"
JOB_URL = API + "/inbound/application-portal-job/{job_id}"
BOARD_URL = API + "/careers-page/{client_id}/jobs"


class DoverFetcher:
    platform: ClassVar[Platform] = Platform.DOVER

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        # Per run: company slug -> Dover client id, learned from fetched jobs.
        self._clients: dict[str, str] = {}
        self._jobs: dict[str, Posting] = {}

    def fetch(self, parsed: ParsedUrl) -> Posting:
        assert parsed.job_id  # guaranteed by url_parser
        return self._fetch_job(parsed.job_id, parsed.url)

    def fetch_board(self, slug: str) -> list[Posting | FetchError]:
        """Every published job at a company seen in this run."""
        results: list[Posting | FetchError] = []
        for job_id in self._list(self._client_id(slug)):
            try:
                results.append(self._fetch_job(job_id))
            except FetchError as exc:
                # Report the job's apply page, not the API call behind it.
                exc.url = f"https://app.dover.com/apply/{slug}/{job_id}"
                results.append(exc)
        return results

    def list_board(self, slug: str) -> list[ParsedUrl]:
        """Every published job at a company as a job URL: the careers page only."""
        return [
            ParsedUrl(
                platform=self.platform,
                slug=slug,
                job_id=job_id.lower(),
                url=f"https://app.dover.com/apply/{slug}/{job_id}",
            )
            for job_id in self._list(self._client_id(slug))
        ]

    def learn(self, parsed: ParsedUrl) -> None:
        """Find a company's client id from one of its jobs: one request.

        Only a job response carries it. A job that is gone teaches nothing;
        the next one shown may still be up.
        """
        if not parsed.job_id or parsed.slug in self._clients:
            return
        try:
            self._fetch_job(parsed.job_id, parsed.url)
        except FetchError:
            pass

    def _client_id(self, slug: str) -> str:
        client_id = self._clients.get(slug)
        if client_id is None:
            raise BoardNotFound(
                f"no Dover company {slug!r} seen in this run -- a Dover board is "
                f"found from one of its job URLs",
                url=f"dover:{slug}",
                platform=self.platform,
            )
        return client_id

    def _list(self, client_id: str) -> list[str]:
        """Job ids on a company's careers page, following pagination."""
        url: str | None = BOARD_URL.format(client_id=client_id)
        ids: list[str] = []
        while url:
            page = self._client.get_json(url, platform=self.platform)
            if page is None:
                raise BoardNotFound(
                    f"no Dover careers page for client {client_id!r}",
                    url=url,
                    platform=self.platform,
                )
            _expect_dict(page, url)
            ids += [
                job["id"]
                for job in page.get("results") or []
                if job.get("id") and job.get("is_published", True) and not job.get("is_sample")
            ]
            url = page.get("next")
        return list(dict.fromkeys(ids))

    def _fetch_job(self, job_id: str, url: str | None = None) -> Posting:
        cached = self._jobs.get(job_id)
        if cached is not None:
            return cached
        api_url = JOB_URL.format(job_id=job_id)
        job = self._client.get_json(api_url, platform=self.platform)
        if job is None:
            raise JobNotFound(
                f"Dover has no job {job_id!r} -- it may have been taken down",
                url=url or f"https://app.dover.com/apply/x/{job_id}",
                platform=self.platform,
            )
        _expect_dict(job, api_url)
        posting = _to_posting(job, url)
        if job.get("client_id"):
            self._clients[posting.company_slug] = job["client_id"]
        self._jobs[job_id] = posting
        return posting


def _expect_dict(payload: Any, url: str) -> None:
    # A path Dover doesn't serve as API falls through to the app's HTML shell.
    if not isinstance(payload, dict):
        raise TransportFailure(f"{url} returned unexpected JSON", url=url, platform=Platform.DOVER)


def _location(job: dict[str, Any]) -> str | None:
    names = [
        (entry.get("location_option") or {}).get("display_name")
        for entry in job.get("locations") or []
    ]
    names = [name for name in dict.fromkeys(names) if name]
    if names:
        return "; ".join(names)
    return "Remote" if job.get("workplace_type") == "REMOTE" else None


def _to_posting(job: dict[str, Any], url: str | None) -> Posting:
    name = job.get("client_name") or ""
    return Posting(
        platform=Platform.DOVER,
        company_slug=slugify_company(name) or str(job.get("client_id") or "unknown"),
        job_id=str(job["id"]).lower(),
        title=job.get("title") or "",
        location=_location(job),
        url=url or f"https://app.dover.com/apply/{quote(name)}/{job['id']}",
        description_text=html_to_text(job.get("user_provided_description") or ""),
        raw=job,
    )
