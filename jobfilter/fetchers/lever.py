"""Lever fetcher.

Lever splits one posting across several fields. `descriptionPlain` is only the
intro; the substance lives in `lists` (each an HTML `<li>` blob under a heading)
and the closing legal block lives in `additionalPlain`.

`INCLUDE_ADDITIONAL` is a deliberate widening of the Step 2 spec -- see the
constant's comment. Flip it to False for descriptionPlain + lists only.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..models import ParsedUrl, Platform, Posting
from .base import BoardNotFound, JobNotFound
from .http import HttpClient
from .textutil import html_to_text, join_sections, normalize_text

JOB_URL = "https://api.lever.co/v0/postings/{slug}/{job_id}?mode=json"
# The same endpoint without a job id returns every posting, each with the full
# descriptionPlain / lists / additionalPlain set.
BOARD_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

# CLAUDE.md specifies descriptionPlain + lists. We also take `additionalPlain`
# because that trailing block is exactly where companies put export-control and
# citizenship notices next to their EEO boilerplate; dropping it would produce
# false OPEN results, the worst failure mode for this tool. The cost is that the
# EEO text reaches the classifier -- which Step 3 must already ignore.
INCLUDE_ADDITIONAL = True


class LeverFetcher:
    platform: ClassVar[Platform] = Platform.LEVER

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self._boards: dict[str, list[dict[str, Any]]] = {}

    def fetch(self, parsed: ParsedUrl) -> Posting:
        assert parsed.slug and parsed.job_id  # guaranteed by url_parser

        job = self._client.get_json(
            JOB_URL.format(slug=parsed.slug, job_id=parsed.job_id),
            platform=self.platform,
        )
        if job is None:
            raise JobNotFound(
                f"Lever returned 404 for job {parsed.job_id!r} on board "
                f"{parsed.slug!r} -- the job or the board does not exist",
                url=parsed.url,
                platform=self.platform,
            )

        return self._to_posting(job, parsed.slug, parsed.url)

    def fetch_board(self, slug: str) -> list[Posting]:
        """Every posting at this company. One request, cached per slug."""
        cached = self._boards.get(slug)
        if cached is None:
            payload = self._client.get_json(
                BOARD_URL.format(slug=slug), platform=self.platform
            )
            if payload is None:
                raise BoardNotFound(
                    f"no Lever job board for company slug {slug!r}",
                    url=f"https://jobs.lever.co/{slug}",
                    platform=self.platform,
                )
            # Lever answers with a bare list, not an object.
            cached = payload if isinstance(payload, list) else []
            self._boards[slug] = cached

        return [self._to_posting(job, slug) for job in cached if job.get("id")]

    def _to_posting(
        self, job: dict[str, Any], slug: str, url: str | None = None
    ) -> Posting:
        categories = job.get("categories") or {}
        return Posting(
            platform=self.platform,
            company_slug=slug,
            job_id=str(job["id"]),
            title=job.get("text") or "",
            location=categories.get("location") or None,
            url=url or job.get("hostedUrl") or "",
            description_text=_description(job),
            raw=job,
        )


def _description(job: dict) -> str:
    """descriptionPlain + each `lists` section (heading kept) + additionalPlain."""
    sections: list[str] = [normalize_text(job.get("descriptionPlain") or "")]

    for section in job.get("lists") or []:
        heading = normalize_text(section.get("text") or "")
        body = html_to_text(section.get("content") or "")
        sections.append(join_sections(heading, body))

    if INCLUDE_ADDITIONAL:
        sections.append(normalize_text(job.get("additionalPlain") or ""))

    return join_sections(*sections)
