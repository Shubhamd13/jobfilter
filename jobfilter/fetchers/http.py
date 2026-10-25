"""One shared HTTP client: timeout, a single retry with backoff, typed errors."""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..models import Platform
from .base import TransportFailure

DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 1  # retries *after* the first attempt, so 2 attempts total
DEFAULT_BACKOFF = 0.5
USER_AGENT = "jobfilter/0.1 (+https://github.com/local/job-restriction-filter)"

# Worth retrying: the server or the wire flaked. A 4xx will not improve.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpClient:
    """Thin wrapper over httpx with the retry policy in one place.

    Tests pass a `transport` (httpx.MockTransport) so the whole stack -- retry
    counting included -- runs offline against fixtures.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def get(
        self,
        url: str,
        *,
        platform: Platform | None = None,
        follow_redirects: bool = True,
    ) -> httpx.Response:
        """GET with one retry on transport errors and retryable status codes.

        Returns the response for any non-retryable status -- including 404, which
        callers turn into BoardNotFound or JobNotFound with their own context.
        """
        return self._send("GET", url, platform, follow_redirects=follow_redirects)

    def post(
        self, url: str, payload: Any, *, platform: Platform | None = None
    ) -> httpx.Response:
        """POST a JSON body, with the same retry policy as `get`.

        Only for read-only search endpoints (Workday's job search is a POST).
        """
        return self._send("POST", url, platform, json=payload)

    def _send(
        self, method: str, url: str, platform: Platform | None, **kwargs: Any
    ) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if response.status_code in _RETRY_STATUS:
                last_error = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
                continue
            return response

        raise TransportFailure(
            f"{method} {url} failed after {self.retries + 1} attempt(s): {last_error}",
            url=url,
            platform=platform,
        ) from last_error

    def get_json(self, url: str, *, platform: Platform | None = None) -> Any:
        """GET and decode JSON. A body that is not JSON is a transport failure."""
        response = self.get(url, platform=platform)
        if response.status_code == 404:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise TransportFailure(
                f"GET {url} returned {response.status_code} with a non-JSON body",
                url=url,
                platform=platform,
            ) from exc

    def pause(self, seconds: float) -> None:
        """Wait, through the injectable sleep: tests pass a no-op."""
        self._sleep(seconds)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
