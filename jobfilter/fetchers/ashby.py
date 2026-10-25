"""Ashby fetcher.

Ashby has no per-job endpoint: one GET returns the company's whole board, so we
cache it per slug. Several URLs from one company therefore cost one request.

A company careers page with an Ashby embed links jobs as
`company.com/careers?ashby_jid={job_id}`, without the board slug, and Ashby
offers no lookup by job id alone (its job page and GraphQL API both need the
slug). The slug is found from the page itself -- the embed script is loaded
from `jobs.ashbyhq.com/{slug}/embed` -- or else guessed from the domain, and
only accepted once that board actually lists the job.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Iterator
from urllib.parse import unquote, urlsplit

from ..models import ParsedUrl, Platform, Posting
from .base import BoardNotFound, FetchError, JobNotFound
from .http import HttpClient
from .textutil import normalize_text

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

# The embed script, iframe or a job link on a company page names the board.
_SLUG_ON_PAGE_RE = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9._%-]*)")
_NOT_SLUGS = frozenset({"api", "embed", "static", "assets"})
# Host labels that never name the company: careers.acme.com, jobs.acme.com.
_GENERIC_HOST_LABELS = frozenset({"www", "careers", "career", "jobs", "job", "apply", "work"})


class AshbyFetcher:
    platform: ClassVar[Platform] = Platform.ASHBY

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        # The per-run fetch cache CLAUDE.md allows. Instance state, not global:
        # a new registry per run starts empty.
        self._boards: dict[str, list[dict[str, Any]]] = {}

    def fetch(self, parsed: ParsedUrl) -> Posting:
        assert parsed.job_id  # guaranteed by url_parser
        slug = parsed.slug or self._resolve_slug(parsed.job_id, parsed.url)
        jobs = self._board(slug, parsed.url)

        job = _find(jobs, parsed.job_id)
        if job is None:
            raise JobNotFound(
                f"job {parsed.job_id!r} is not on the Ashby board for "
                f"{slug!r} ({len(jobs)} job(s) listed) -- it may be "
                f"closed or unlisted",
                url=parsed.url,
                platform=self.platform,
            )

        return self._to_posting(job, slug, parsed.url)

    def _resolve_slug(self, job_id: str, url: str) -> str:
        """Find the board for an `?ashby_jid=` URL on a company's own site."""
        tried: list[str] = []
        for slug in self._slug_candidates(url):
            if slug in tried:
                continue
            tried.append(slug)
            try:
                jobs = self._board(slug, url)
            except BoardNotFound:
                continue
            if _find(jobs, job_id) is not None:
                return slug
        raise JobNotFound(
            f"Ashby job {job_id!r}: the URL does not name its board and none "
            f"found lists it (tried {', '.join(tried) or 'nothing'}) -- the job "
            f"may be closed, or the board named unlike the page and domain",
            url=url,
            platform=self.platform,
        )

    def _slug_candidates(self, url: str) -> Iterator[str]:
        """Slugs named on the page first, then guesses from the domain."""
        try:
            response = self._client.get(url.split("#", 1)[0], platform=self.platform)
        except FetchError:
            response = None
        if response is not None and response.status_code == 200:
            for match in _SLUG_ON_PAGE_RE.finditer(response.text):
                slug = unquote(match.group(1))
                if slug.lower() not in _NOT_SLUGS:
                    yield slug
        yield from _domain_slugs(url)

    def fetch_board(self, slug: str) -> list[Posting]:
        """Every job at this company.

        Free: `fetch` already pulled the whole board to find one job, so
        expanding a company we have touched costs no extra request.
        """
        return [
            self._to_posting(job, slug)
            for job in self._board(slug, f"https://jobs.ashbyhq.com/{slug}")
            if job.get("id")
        ]

    def _to_posting(
        self, job: dict[str, Any], slug: str, url: str | None = None
    ) -> Posting:
        return Posting(
            platform=self.platform,
            company_slug=slug,
            job_id=str(job["id"]),
            title=job.get("title") or "",
            location=job.get("location") or None,
            # Discovered jobs have no URL from the user; the board gives one.
            url=url or job.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}",
            # Ashby hands us plain text already; only whitespace needs work.
            description_text=normalize_text(job.get("descriptionPlain") or ""),
            raw=job,
        )

    def _board(self, slug: str, url: str) -> list[dict[str, Any]]:
        cached = self._boards.get(slug)
        if cached is not None:
            return cached

        payload = self._client.get_json(
            BOARD_URL.format(slug=slug), platform=self.platform
        )
        if payload is None:
            raise BoardNotFound(
                f"no Ashby job board for company slug {slug!r}",
                url=url,
                platform=self.platform,
            )

        jobs = payload.get("jobs") or []
        self._boards[slug] = jobs
        return jobs


def _find(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    return next((j for j in jobs if str(j.get("id", "")).lower() == job_id.lower()), None)


def _domain_slugs(url: str) -> Iterator[str]:
    """www.corvus-robotics.com -> corvus-robotics; careers.acme.io -> acme."""
    host = (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()
    labels = [label for label in host.split(".")[:-1] if label not in _GENERIC_HOST_LABELS]
    yield from labels
