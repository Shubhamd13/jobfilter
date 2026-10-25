"""Per-platform fetchers, all behind one `fetch(ParsedUrl) -> Posting` interface.

Ashby, Greenhouse, Lever and Workday all have public JSON APIs. A
Playwright-backed fetcher for career pages without one would register in
`registry.py`; nothing else needs to change.
"""

from .base import (
    BoardFetcher,
    BoardLearner,
    BoardLister,
    BoardNotFound,
    BoardUrlFetcher,
    Fetcher,
    FetchError,
    JobNotFound,
    RateLimited,
    TransportFailure,
    UnsupportedPlatform,
)
from .http import HttpClient
from .registry import FetcherRegistry

__all__ = [
    "BoardFetcher",
    "BoardLearner",
    "BoardLister",
    "BoardNotFound",
    "BoardUrlFetcher",
    "Fetcher",
    "FetchError",
    "FetcherRegistry",
    "HttpClient",
    "JobNotFound",
    "RateLimited",
    "TransportFailure",
    "UnsupportedPlatform",
]
