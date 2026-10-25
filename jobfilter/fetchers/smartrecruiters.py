"""SmartRecruiters fetcher.

SmartRecruiters publishes a company's postings as unauthenticated JSON, and the
company identifier is in the job page's URL
(jobs.smartrecruiters.com/{Company}/{job_id}-{title}):

    GET api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}          one job
    GET api.smartrecruiters.com/v1/companies/{company}/postings?offset=0&limit=100  the board, no text

The list comes a hundred at a time (a larger limit is cut to 100) and its
entries carry no description, so a whole board costs one request per job on
top of the list. The identifier is case-insensitive; the slug stored is its
lower-case form.

A job's text is in `jobAd.sections`: company description, job description,
qualifications and additional information, each HTML under its own title. All
are kept, in order, under those titles. "Additional Information" is where a
company puts its export-control and sponsorship statements.

An unknown company is not a 404: its list is simply empty. That is reported as
a missing board rather than as a board with no jobs, so a refresh never takes
it to mean every stored job was closed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar
from urllib.parse import quote

from ..models import ParsedUrl, Platform, Posting
from .base import BoardNotFound, FetchError, JobNotFound, TransportFailure
from .http import HttpClient
from .textutil import html_to_text, join_sections

API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
SITE = "https://jobs.smartrecruiters.com"
PAGE = 100
WORKERS = 6


class SmartRecruitersFetcher:
    platform: ClassVar[Platform] = Platform.SMARTRECRUITERS

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

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            return list(pool.map(one, job_ids))

    def list_board(self, slug: str) -> list[ParsedUrl]:
        """Every job on a board as a job URL: the list requests only."""
        return [
            ParsedUrl(
                platform=self.platform, slug=slug, job_id=job_id, url=f"{SITE}/{slug}/{job_id}"
            )
            for job_id in self._list(slug)
        ]

    def fetch_board_url(self, parsed: ParsedUrl) -> list[Posting | FetchError]:
        """`jobs.smartrecruiters.com/{Company}`: the company is all a board needs."""
        assert parsed.slug  # guaranteed by url_parser
        return self.fetch_board(parsed.slug)

    def _list(self, slug: str) -> list[str]:
        cached = self._boards.get(slug)
        if cached is not None:
            return cached

        base = API.format(company=quote(slug))
        job_ids: list[str] = []
        offset = 0
        while True:
            url = f"{base}?offset={offset}&limit={PAGE}"
            page = self._client.get_json(url, platform=self.platform)
            if not isinstance(page, dict):
                raise TransportFailure(f"{url} returned unexpected JSON", url=url, platform=self.platform)
            entries = [e for e in page.get("content") or [] if isinstance(e, dict) and e.get("id")]
            job_ids.extend(str(e["id"]) for e in entries)
            offset += PAGE
            if not entries or offset >= int(page.get("totalFound") or 0):
                break

        if not job_ids:
            # SmartRecruiters answers an unknown company with an empty list, not a 404.
            raise BoardNotFound(
                f"SmartRecruiters lists no postings for company {slug!r} -- "
                f"the company does not exist there, or has nothing open",
                url=f"{SITE}/{slug}",
                platform=self.platform,
            )
        job_ids = list(dict.fromkeys(job_ids))
        self._boards[slug] = job_ids
        return job_ids

    def _fetch_job(self, slug: str, job_id: str, url: str | None = None) -> Posting:
        cached = self._jobs.get((slug, job_id))
        if cached is not None:
            return cached

        page_url = url or f"{SITE}/{slug}/{job_id}"
        api_url = f"{API.format(company=quote(slug))}/{quote(job_id)}"
        job = self._client.get_json(api_url, platform=self.platform)
        if job is None:
            raise JobNotFound(
                f"SmartRecruiters has no posting {job_id!r} for company {slug!r} -- "
                f"it may have been filled or taken down",
                url=page_url,
                platform=self.platform,
            )
        if not isinstance(job, dict):
            raise TransportFailure(f"{api_url} returned unexpected JSON", url=page_url, platform=self.platform)

        posting = _to_posting(job, slug, job_id, page_url)
        self._jobs[(slug, job_id)] = posting
        return posting


def _to_posting(job: dict[str, Any], slug: str, job_id: str, page_url: str) -> Posting:
    released = str(job.get("releasedDate") or "")[:10]
    return Posting(
        platform=Platform.SMARTRECRUITERS,
        company_slug=slug,
        job_id=job_id,
        title=(job.get("name") or "").strip(),
        location=_location(job.get("location")),
        # The job's own page, without the tracking query a pasted link carries.
        url=job.get("postingUrl") or page_url,
        description_text=join_sections(*_sections(job)),
        raw=job,
        posted_ago=f"Posted {released}" if released else None,
    )


def _sections(job: dict[str, Any]) -> list[str]:
    """Each section of the ad as "Title\\ntext", in the order the page shows them."""
    sections = (job.get("jobAd") or {}).get("sections") or {}
    parts: list[str] = []
    for section in sections.values():
        if not isinstance(section, dict):
            continue
        text = html_to_text(section.get("text") or "")
        if text:
            title = (section.get("title") or "").strip()
            parts.append(f"{title}\n{text}" if title else text)
    return parts


def _location(location: Any) -> str | None:
    """"Sunnyvale, CA, United States"; a remote job says so first."""
    if not isinstance(location, dict):
        return None
    place = location.get("fullLocation") or ", ".join(
        str(part) for part in (location.get("city"), location.get("region"), location.get("country")) if part
    )
    if location.get("remote"):
        return f"Remote - {place}" if place else "Remote"
    return place or None
