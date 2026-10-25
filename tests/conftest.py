"""Offline fixture plumbing.

Every test below serves saved real responses through `httpx.MockTransport`, so
the production code path -- httpx, retry policy, JSON decoding -- runs unchanged
without touching the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from jobfilter.fetchers import FetcherRegistry, HttpClient

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_PROFILE = Path(__file__).parent.parent / "profile.example.json"


@pytest.fixture(autouse=True)
def _never_the_developers_own_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """profile.json is personal and git-ignored: tests read the checked-in example instead."""
    monkeypatch.setenv("JOBFILTER_PROFILE", str(EXAMPLE_PROFILE))

# The three real postings saved in tests/fixtures/.
ASHBY_SLUG = "graymatter-robotics"
ASHBY_JOB_ID = "dea0f08d-ebf5-4e55-b9cb-e6302014f429"
ASHBY_URL = f"https://jobs.ashbyhq.com/{ASHBY_SLUG}/{ASHBY_JOB_ID}"

GH_SLUG = "undercontrolroboticsinc"
GH_JOB_ID = "4396196009"
GH_URL = f"https://job-boards.greenhouse.io/{GH_SLUG}/jobs/{GH_JOB_ID}"

LEVER_SLUG = "rai"
LEVER_JOB_ID = "dff73976-3e91-4897-af10-8d2433c63204"
LEVER_URL = f"https://jobs.lever.co/{LEVER_SLUG}/{LEVER_JOB_ID}"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def ashby_sibling_job_id() -> str:
    """Another job on the same Ashby board, for the cache test."""
    jobs = load_fixture("ashby_board_graymatter-robotics.json")["jobs"]
    return next(j["id"] for j in jobs if j["id"] != ASHBY_JOB_ID)


# API URL -> fixture file. Anything not listed here answers 404.
_ROUTES = {
    f"https://api.ashbyhq.com/posting-api/job-board/{ASHBY_SLUG}":
        "ashby_board_graymatter-robotics.json",
    f"https://boards-api.greenhouse.io/v1/boards/{GH_SLUG}/jobs/{GH_JOB_ID}":
        "greenhouse_undercontrolroboticsinc_4396196009.json",
    f"https://api.lever.co/v0/postings/{LEVER_SLUG}/{LEVER_JOB_ID}?mode=json":
        "lever_rai_dff73976-3e91-4897-af10-8d2433c63204.json",
    # Whole-board endpoints, used by --expand.
    f"https://boards-api.greenhouse.io/v1/boards/{GH_SLUG}/jobs?content=true":
        "greenhouse_board_undercontrolroboticsinc.json",
    f"https://api.lever.co/v0/postings/{LEVER_SLUG}?mode=json":
        "lever_board_rai.json",
}

# Greenhouse slug lookup by job id: URL -> redirect Location (real response shape).
_REDIRECTS = {
    f"https://boards.greenhouse.io/embed/job_app?token={GH_JOB_ID}":
        f"https://job-boards.greenhouse.io/embed/job_app?for={GH_SLUG}&token={GH_JOB_ID}",
}

# Job counts in the saved board fixtures.
ASHBY_BOARD_SIZE = 16
GH_BOARD_SIZE = 10
LEVER_BOARD_SIZE = 4


class RecordingTransport(httpx.MockTransport):
    """MockTransport that counts requests, so caching can be asserted."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        location = _REDIRECTS.get(url)
        if location is not None:
            return httpx.Response(301, headers={"location": location})
        fixture = _ROUTES.get(url)
        if fixture is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=load_fixture(fixture))


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def client(transport: RecordingTransport) -> HttpClient:
    # backoff 0 and a no-op sleep keep retry tests instant.
    return HttpClient(transport=transport, backoff=0.0, sleep=lambda _: None)


@pytest.fixture
def registry(client: HttpClient) -> FetcherRegistry:
    return FetcherRegistry(client)
