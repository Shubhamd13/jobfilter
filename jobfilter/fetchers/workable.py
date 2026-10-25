"""Workable fetcher.

Workable shows a job in two places, with two different ids:

    apply.workable.com/{account}/j/{shortcode}      the company's own careers site
    jobs.workable.com/view/{view_id}/{title}        Workable's job board, across companies

The careers site has the better API. One unauthenticated request returns every
job of an account with its full text (description, requirements and benefits
in one HTML field):

    GET apply.workable.com/api/v1/widget/accounts/{account}?details=true

so, as with Ashby, a single job is read from its company's board, and several
jobs of one company cost one request. A job is stored under its account and
shortcode whichever link it came from.

A job-board link names neither. Its job is looked up

    GET jobs.workable.com/api/v1/jobs/{view_id}

which gives the title and the company ("jobs-at-octavian-robotics"); the
account is taken from that, and the job is found on the account's board by its
title. If the account cannot be worked out that way (the company's account
name differs from its display name), the posting is still returned, built from
the job-board answer and filed under the view id.

A short link (apply.workable.com/j/{shortcode}) names no account either; it
redirects to the full address, and the account is read from the redirect.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from ..models import ParsedUrl, Platform, Posting
from .base import BoardNotFound, FetchError, JobNotFound, TransportFailure
from .http import HttpClient
from .textutil import html_to_text, join_sections

APPLY = "https://apply.workable.com"
BOARD_API = APPLY + "/api/v1/widget/accounts/{account}?details=true"
VIEW_API = "https://jobs.workable.com/api/v1/jobs/{view_id}"
# A careers-site shortcode is ten hex digits; a job-board view id is longer and mixed-case.
SHORTCODE = re.compile(r"[0-9A-Fa-f]{10}")
_COMPANY_PAGE = re.compile(r"/jobs-at-([a-z0-9-]+)/?$", re.IGNORECASE)


class WorkableFetcher:
    platform: ClassVar[Platform] = Platform.WORKABLE

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self._boards: dict[str, list[Posting]] = {}

    def fetch(self, parsed: ParsedUrl) -> Posting:
        assert parsed.job_id  # guaranteed by url_parser
        if not SHORTCODE.fullmatch(parsed.job_id):
            return self._fetch_view(parsed.job_id, parsed.url)

        shortcode = parsed.job_id.upper()
        account = parsed.slug or self._account_of(shortcode, parsed.url)
        for posting in self._board(account):
            if posting.job_id == shortcode:
                return posting
        raise JobNotFound(
            f"Workable account {account!r} lists no job {shortcode!r} -- "
            f"it may have been filled or taken down",
            url=parsed.url,
            platform=self.platform,
        )

    def fetch_board(self, slug: str) -> list[Posting | FetchError]:
        """Every job of one account: the single board request."""
        return list(self._board(slug))

    def fetch_board_url(self, parsed: ParsedUrl) -> list[Posting | FetchError]:
        """`apply.workable.com/{account}`: the account is all a board needs."""
        assert parsed.slug  # guaranteed by url_parser
        return self.fetch_board(parsed.slug)

    def _board(self, account: str) -> list[Posting]:
        cached = self._boards.get(account)
        if cached is not None:
            return cached
        url = BOARD_API.format(account=quote(account))
        board = self._client.get_json(url, platform=self.platform)
        if board is None:
            raise BoardNotFound(
                f"no Workable careers site for account {account!r}",
                url=f"{APPLY}/{account}/",
                platform=self.platform,
            )
        if not isinstance(board, dict) or not isinstance(board.get("jobs"), list):
            raise TransportFailure(f"{url} returned unexpected JSON", url=url, platform=self.platform)
        postings = [
            _to_posting(job, account, board.get("name"))
            for job in board["jobs"]
            if isinstance(job, dict) and job.get("shortcode")
        ]
        self._boards[account] = postings
        return postings

    def _account_of(self, shortcode: str, url: str) -> str:
        """The account a short link (`/j/{shortcode}`) redirects to."""
        link = f"{APPLY}/j/{shortcode}"
        response = self._client.get(link, platform=self.platform, follow_redirects=False)
        segments = [s for s in urlsplit(response.headers.get("location", "")).path.split("/") if s]
        if response.status_code in (301, 302, 307, 308) and len(segments) >= 3 and segments[1] == "j":
            return segments[0].lower()
        raise JobNotFound(
            f"Workable has no job {shortcode!r} -- it may have been filled or taken down",
            url=url,
            platform=self.platform,
        )

    def _fetch_view(self, view_id: str, url: str) -> Posting:
        """A job-board link: find the job, then find it again on its company's board."""
        view = self._client.get_json(VIEW_API.format(view_id=quote(view_id)), platform=self.platform)
        if view is None:
            raise JobNotFound(
                f"Workable's job board has no job {view_id!r} -- it may have been filled or taken down",
                url=url,
                platform=self.platform,
            )
        if not isinstance(view, dict):
            raise TransportFailure(f"job {view_id!r} returned unexpected JSON", url=url, platform=self.platform)

        company = view.get("company") if isinstance(view.get("company"), dict) else {}
        named = _COMPANY_PAGE.search(str(company.get("url") or ""))
        account = named.group(1).lower() if named else None
        if account:
            try:
                same = [p for p in self._board(account) if _same(p.title, view.get("title"))]
            except BoardNotFound:
                same = []
            if len(same) > 1:
                # Two openings with one title: tell them apart by city.
                city = _same_key((view.get("location") or {}).get("city"))
                same = [p for p in same if city and city in _same_key(p.location)] or same
            if same:
                return same[0]
        return _view_posting(view, view_id, account, url)


def _same_key(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _same(a: Any, b: Any) -> bool:
    return bool(_same_key(a)) and _same_key(a) == _same_key(b)


def _to_posting(job: dict[str, Any], account: str, company: Any) -> Posting:
    shortcode = str(job["shortcode"]).upper()
    published = str(job.get("published_on") or "")[:10]
    return Posting(
        platform=Platform.WORKABLE,
        company_slug=account,
        job_id=shortcode,
        title=(job.get("title") or "").strip(),
        location=_location(job),
        url=f"{APPLY}/{account}/j/{shortcode}",
        description_text=html_to_text(job.get("description") or ""),
        # The account's display name sits one level above the job in the
        # board answer; it is added here so the name travels with the posting.
        raw={**job, "company_name": company} if isinstance(company, str) and company else job,
        posted_ago=f"Posted {published}" if published else None,
    )


def _location(job: dict[str, Any]) -> str | None:
    """"Los Angeles, California, United States", several joined by "; "; remote says so."""
    places: list[str] = []
    for place in job.get("locations") or [job]:
        if not isinstance(place, dict):
            continue
        parts = [place.get("city"), place.get("region") or place.get("state"), place.get("country")]
        text = ", ".join(str(p) for p in parts if p)
        if text and text.lower() not in {seen.lower() for seen in places}:
            places.append(text)
    where = "; ".join(places)
    if job.get("telecommuting") is True:
        return f"Remote - {where}" if where else "Remote"
    return where or None


def _view_posting(view: dict[str, Any], view_id: str, account: str | None, url: str) -> Posting:
    """A posting from the job board's own answer, when the careers site could not be matched."""
    company = view.get("company") if isinstance(view.get("company"), dict) else {}
    title = company.get("title")
    places = [str(p) for p in view.get("locations") or [] if p]
    created = str(view.get("created") or "")[:10]
    return Posting(
        platform=Platform.WORKABLE,
        company_slug=account or _same_key(title).replace(" ", "-") or "workable",
        job_id=view_id,
        title=(view.get("title") or "").strip(),
        location=("Remote - " if view.get("workplace") == "remote" else "") + "; ".join(places) or None,
        url=view.get("url") or url,
        description_text=join_sections(
            html_to_text(view.get("description") or ""),
            html_to_text(view.get("requirementsSection") or ""),
            html_to_text(view.get("benefitsSection") or ""),
        ),
        raw={**view, "company_name": title} if isinstance(title, str) and title else view,
        posted_ago=f"Posted {created}" if created else None,
    )
