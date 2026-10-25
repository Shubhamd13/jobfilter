"""The fetcher interface and its error taxonomy.

Every fetcher -- HTTP today, Playwright later for sites with no API -- implements `fetch`.
Errors are typed so the CLI can tell "this company board is gone" apart from
"this one job is gone" apart from "the network flaked", and report each posting
rather than silently dropping it.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, Sequence, runtime_checkable

from ..models import ParsedUrl, Platform, Posting


class FetchError(Exception):
    """Base class for every fetch failure. Always carries the offending URL."""

    def __init__(self, message: str, *, url: str, platform: Platform | None = None):
        super().__init__(message)
        self.url = url
        self.platform = platform


class BoardNotFound(FetchError):
    """The company slug does not exist on that platform (board-level 404)."""


class JobNotFound(FetchError):
    """The board exists but holds no job with this id (job-level 404)."""


class TransportFailure(FetchError):
    """Network error, timeout or 5xx that survived the retry."""


class RateLimited(TransportFailure):
    """The site kept refusing us for asking too often; nothing more is sent to it this run."""


class UnsupportedPlatform(FetchError):
    """No fetcher is registered for this platform."""


@runtime_checkable
class Fetcher(Protocol):
    """Turn a ParsedUrl into a normalized Posting."""

    platform: ClassVar[Platform]

    def fetch(self, parsed: ParsedUrl) -> Posting: ...


@runtime_checkable
class BoardFetcher(Fetcher, Protocol):
    """A fetcher that can also list every job at a company.

    Optional on purpose: every platform so far publishes a whole-board
    endpoint, but a browser fetcher scraping one custom careers page cannot,
    and should not be forced to pretend. `FetcherRegistry.supports_boards`
    answers the question at runtime.

    Where a board needs one request per job (Workday), a job that fails comes
    back as its FetchError in place of a Posting, so it is reported, not dropped.
    """

    def fetch_board(self, slug: str) -> Sequence[Posting | FetchError]: ...


@runtime_checkable
class BoardUrlFetcher(BoardFetcher, Protocol):
    """A board fetcher that also takes a board URL as input.

    Workday: `nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite?...`
    names the site, a search and facets -- more than a slug can carry.
    """

    def fetch_board_url(self, parsed: ParsedUrl) -> Sequence[Posting | FetchError]: ...


@runtime_checkable
class BoardLister(BoardFetcher, Protocol):
    """A board fetcher that can name a board's jobs without fetching them.

    For platforms where a board costs one request per job (Workday, Rippling,
    Dover): a refresh lists the board, then fetches only the jobs it does not
    hold yet. Each reference is a job URL `fetch` accepts. Ashby, Greenhouse
    and Lever are not listers -- their board arrives whole in one request.
    """

    def list_board(self, slug: str) -> Sequence[ParsedUrl]: ...


@runtime_checkable
class BoardLearner(Protocol):
    """A fetcher that finds a board from one of its jobs, not from the slug.

    A Workday board lives at a host and site name, a Dover one under a client
    id; a run learns them from the job URLs it is given. A refresh starts from
    stored postings instead, so it shows the fetcher their URLs first.
    """

    def learn(self, parsed: ParsedUrl) -> None: ...
