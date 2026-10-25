"""Platform -> fetcher dispatch, owning the one HTTP client for a run."""

from __future__ import annotations

from typing import Sequence

from ..models import ParsedUrl, Platform, Posting
from .amazon import AmazonFetcher
from .ashby import AshbyFetcher
from .base import (
    BoardFetcher,
    BoardLearner,
    BoardLister,
    BoardUrlFetcher,
    Fetcher,
    FetchError,
    UnsupportedPlatform,
)
from .dover import DoverFetcher
from .eightfold import EightfoldFetcher
from .greenhouse import GreenhouseFetcher
from .http import HttpClient
from .lever import LeverFetcher
from .rippling import RipplingFetcher
from .smartrecruiters import SmartRecruitersFetcher
from .workable import WorkableFetcher
from .workday import WorkdayFetcher


class FetcherRegistry:
    """Build one per run: it owns the HTTP client and the per-run caches.

    A Playwright fetcher for career pages with no API would register here too;
    callers keep calling `registry.fetch(parsed)`.
    """

    def __init__(self, client: HttpClient | None = None) -> None:
        self._client = client or HttpClient()
        self._fetchers: dict[Platform, Fetcher] = {
            Platform.ASHBY: AshbyFetcher(self._client),
            Platform.GREENHOUSE: GreenhouseFetcher(self._client),
            Platform.LEVER: LeverFetcher(self._client),
            Platform.WORKDAY: WorkdayFetcher(self._client),
            Platform.DOVER: DoverFetcher(self._client),
            Platform.RIPPLING: RipplingFetcher(self._client),
            Platform.AMAZON: AmazonFetcher(self._client),
            Platform.SMARTRECRUITERS: SmartRecruitersFetcher(self._client),
            Platform.WORKABLE: WorkableFetcher(self._client),
            Platform.EIGHTFOLD: EightfoldFetcher(self._client),
        }

    def fetcher_for(self, platform: Platform) -> Fetcher:
        fetcher = self._fetchers.get(platform)
        if fetcher is None:
            raise UnsupportedPlatform(
                f"no fetcher registered for platform {platform!r}",
                url="",
                platform=platform,
            )
        return fetcher

    def fetch(self, parsed: ParsedUrl) -> Posting:
        if not parsed.supported:
            raise UnsupportedPlatform(
                f"unsupported URL: {parsed.url!r}",
                url=parsed.url,
                platform=parsed.platform,
            )
        return self.fetcher_for(parsed.platform).fetch(parsed)

    def supports_boards(self, platform: Platform) -> bool:
        """Can this platform list every job at a company?

        True for every API platform. A browser fetcher scraping a single page
        would answer False, and expansion would skip it.
        """
        return isinstance(self._fetchers.get(platform), BoardFetcher)

    def board_platforms(self) -> list[Platform]:
        """Every platform that can list a company's jobs: what a refresh covers."""
        return [platform for platform in self._fetchers if self.supports_boards(platform)]

    def lists_boards(self, platform: Platform) -> bool:
        """Can this platform name a board's jobs without fetching each one?"""
        return isinstance(self._fetchers.get(platform), BoardLister)

    def list_board(self, platform: Platform, slug: str) -> Sequence[ParsedUrl]:
        """Every job at one company as a job URL. Raises UnsupportedPlatform if it cannot."""
        fetcher = self.fetcher_for(platform)
        if not isinstance(fetcher, BoardLister):
            raise UnsupportedPlatform(
                f"{platform} cannot list a board without fetching it",
                url="",
                platform=platform,
            )
        return fetcher.list_board(slug)

    def learn(self, parsed: ParsedUrl) -> None:
        """Show a fetcher a stored job's URL, if its boards are found from jobs."""
        fetcher = self._fetchers.get(parsed.platform)
        if isinstance(fetcher, BoardLearner):
            fetcher.learn(parsed)

    def fetch_board(self, platform: Platform, slug: str) -> Sequence[Posting | FetchError]:
        """Every job at one company. Raises UnsupportedPlatform if it cannot."""
        fetcher = self.fetcher_for(platform)
        if not isinstance(fetcher, BoardFetcher):
            raise UnsupportedPlatform(
                f"{platform} cannot list a whole board",
                url="",
                platform=platform,
            )
        return fetcher.fetch_board(slug)

    def fetch_board_url(self, parsed: ParsedUrl) -> Sequence[Posting | FetchError]:
        """Every job on the board a board URL shows (`parsed.is_board`)."""
        fetcher = self.fetcher_for(parsed.platform)
        if not isinstance(fetcher, BoardUrlFetcher):
            raise UnsupportedPlatform(
                f"{parsed.platform} board URLs are not supported: give a job URL",
                url=parsed.url,
                platform=parsed.platform,
            )
        return fetcher.fetch_board_url(parsed)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FetcherRegistry:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
