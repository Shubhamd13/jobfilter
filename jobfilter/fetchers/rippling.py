"""Rippling ATS fetcher.

Rippling's job pages (ats.rippling.com/{slug}/jobs/{job_id}) are backed by
unauthenticated JSON, and the board slug is in the URL:

    GET api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{job_id}   one job
    GET api.rippling.com/platform/api/ats/v1/board/{slug}/jobs            every job, no descriptions

The list is the whole board in one response (no paging), with one entry per
job *location*: Lyte's 73 entries are 52 jobs. A job's description comes in
two HTML parts, `company` and `role`; both are classified, as on the page.
The application form's questions ("Are you legally authorized to work...")
stay in `raw`: they are asked of every applicant, not stated by the posting.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

from ..models import ParsedUrl, Platform, Posting
from .base import BoardNotFound, FetchError, JobNotFound, TransportFailure
from .http import HttpClient
from .textutil import html_to_text, join_sections

API = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
WORKERS = 6


class RipplingFetcher:
    platform: ClassVar[Platform] = Platform.RIPPLING

    def __init__(self, client: HttpClient, workers: int = WORKERS) -> None:
        self._client = client
        self._workers = workers
        self._boards: dict[str, list[str]] = {}
        self._jobs: dict[tuple[str, str], Posting] = {}

    def fetch(self, parsed: ParsedUrl) -> Posting:
        assert parsed.slug and parsed.job_id  # guaranteed by url_parser
        return self._fetch_job(parsed.slug, parsed.job_id, parsed.url)

    def fetch_board(self, slug: str) -> list[Posting | FetchError]:
        """Every job on a board, one request each. A failed job is returned as its error."""
        job_ids = self._list(slug)

        def one(job_id: str) -> Posting | FetchError:
            try:
                return self._fetch_job(slug, job_id)
            except FetchError as exc:
                return exc

        if not job_ids:
            return []
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            return list(pool.map(one, job_ids))

    def list_board(self, slug: str) -> list[ParsedUrl]:
        """Every job on a board as a job URL: the one list request."""
        return [
            ParsedUrl(
                platform=self.platform,
                slug=slug,
                job_id=job_id.lower(),
                url=f"https://ats.rippling.com/{slug}/jobs/{job_id}",
            )
            for job_id in self._list(slug)
        ]

    def fetch_board_url(self, parsed: ParsedUrl) -> list[Posting | FetchError]:
        """`ats.rippling.com/{slug}/jobs`: the slug is all a board needs."""
        assert parsed.slug  # guaranteed by url_parser
        return self.fetch_board(parsed.slug)

    def _list(self, slug: str) -> list[str]:
        cached = self._boards.get(slug)
        if cached is not None:
            return cached
        url = API.format(slug=slug)
        entries = self._client.get_json(url, platform=self.platform)
        if entries is None:
            raise BoardNotFound(
                f"no Rippling job board for company slug {slug!r}",
                url=f"https://ats.rippling.com/{slug}/jobs",
                platform=self.platform,
            )
        if not isinstance(entries, list):
            raise TransportFailure(f"{url} returned unexpected JSON", url=url, platform=self.platform)
        # One entry per location; the job is the uuid.
        job_ids = list(dict.fromkeys(e["uuid"] for e in entries if isinstance(e, dict) and e.get("uuid")))
        self._boards[slug] = job_ids
        return job_ids

    def _fetch_job(self, slug: str, job_id: str, url: str | None = None) -> Posting:
        cached = self._jobs.get((slug, job_id))
        if cached is not None:
            return cached
        page_url = url or f"https://ats.rippling.com/{slug}/jobs/{job_id}"
        api_url = f"{API.format(slug=slug)}/{job_id}"
        job = self._client.get_json(api_url, platform=self.platform)
        if job is None:
            # Rippling answers 404 for an unknown board and an unknown job alike.
            raise JobNotFound(
                f"Rippling returned 404 for job {job_id!r} on board {slug!r} -- "
                f"the job or the board does not exist",
                url=page_url,
                platform=self.platform,
            )
        if not isinstance(job, dict):
            raise TransportFailure(f"{api_url} returned unexpected JSON", url=page_url, platform=self.platform)
        posting = _to_posting(job, slug, page_url)
        self._jobs[(slug, job_id)] = posting
        return posting


def _to_posting(job: dict[str, Any], slug: str, url: str) -> Posting:
    description = job.get("description") or {}
    locations = [loc for loc in dict.fromkeys(job.get("workLocations") or []) if loc]
    return Posting(
        platform=Platform.RIPPLING,
        company_slug=slug,
        job_id=str(job.get("uuid") or "").lower(),
        title=job.get("name") or "",
        location="; ".join(locations) or None,
        url=url,
        description_text=join_sections(
            html_to_text(description.get("company") or ""),
            html_to_text(description.get("role") or ""),
        ),
        raw=job,
    )
