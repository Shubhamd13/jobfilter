"""Greenhouse fetcher.

URLs from a company's own careers page (`company.com/careers?gh_jid=123`) carry
the job id but not the board slug. Greenhouse's legacy embed endpoint redirects
`boards.greenhouse.io/embed/job_app?token={job_id}` to a URL with `for={slug}`,
so one redirect-free request recovers the slug.

`content` arrives as HTML that has been entity-escaped a second time, so the
body literally contains `&lt;p&gt;`. Unescape once, then strip tags.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from ..models import ParsedUrl, Platform, Posting
from .base import BoardNotFound, JobNotFound
from .http import HttpClient
from .textutil import html_to_text

JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
# `content=true` returns every job WITH its description in one request; without
# it the payload is titles only and each job would need its own fetch.
# Answers 301 -> job-boards.greenhouse.io/embed/job_app?for={slug}&token={job_id},
# or 404 for an unknown job id.
SLUG_LOOKUP_URL = "https://boards.greenhouse.io/embed/job_app?token={job_id}"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


class GreenhouseFetcher:
    platform: ClassVar[Platform] = Platform.GREENHOUSE

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self._boards: dict[str, list[dict[str, Any]]] = {}

    def fetch(self, parsed: ParsedUrl) -> Posting:
        assert parsed.job_id  # guaranteed by url_parser
        slug = parsed.slug or self._resolve_slug(parsed.job_id, parsed.url)

        job = self._client.get_json(
            JOB_URL.format(slug=slug, job_id=parsed.job_id),
            platform=self.platform,
        )
        if job is None:
            # Greenhouse answers 404 for an unknown board and an unknown job
            # alike, so we cannot tell the two apart without a second request.
            raise JobNotFound(
                f"Greenhouse returned 404 for job {parsed.job_id!r} on board "
                f"{slug!r} -- the job or the board does not exist",
                url=parsed.url,
                platform=self.platform,
            )

        return self._to_posting(job, slug, parsed.url)

    def _resolve_slug(self, job_id: str, url: str) -> str:
        """Find which company board a Greenhouse job id belongs to."""
        response = self._client.get(
            SLUG_LOOKUP_URL.format(job_id=job_id),
            platform=self.platform,
            follow_redirects=False,
        )
        location = response.headers.get("location", "")
        slug = parse_qs(urlsplit(location).query).get("for", [""])[0].strip()
        if not response.is_redirect or not slug:
            raise JobNotFound(
                f"Greenhouse has no job {job_id!r} (slug lookup answered "
                f"HTTP {response.status_code}) -- it may have been taken down",
                url=url,
                platform=self.platform,
            )
        return slug

    def fetch_board(self, slug: str) -> list[Posting]:
        """Every job at this company. One request, cached per slug."""
        cached = self._boards.get(slug)
        if cached is None:
            payload = self._client.get_json(
                BOARD_URL.format(slug=slug), platform=self.platform
            )
            if payload is None:
                raise BoardNotFound(
                    f"no Greenhouse job board for company slug {slug!r}",
                    url=f"https://job-boards.greenhouse.io/{slug}",
                    platform=self.platform,
                )
            cached = payload.get("jobs") or []
            self._boards[slug] = cached

        return [self._to_posting(job, slug) for job in cached if job.get("id")]

    def _to_posting(
        self, job: dict[str, Any], slug: str, url: str | None = None
    ) -> Posting:
        location = job.get("location") or {}
        return Posting(
            platform=self.platform,
            company_slug=slug,
            job_id=str(job["id"]),
            title=job.get("title") or "",
            location=location.get("name") or None,
            url=url or job.get("absolute_url") or "",
            description_text=html_to_text(job.get("content") or "", unescape_first=True),
            raw=job,
        )
