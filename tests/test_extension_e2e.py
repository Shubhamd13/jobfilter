"""End to end: the real extension in a real (headless) Chrome.

The unit tests in test_extension.py drive each JS file with fakes. These load
the unpacked extension exactly as a user would, serve saved fixture pages at
the real LinkedIn / Indeed URLs (no network), and read the badge the content
script draws. In server mode, they run a real jobfilter server and check that
the posting reached its database.

Skipped when Chrome is missing or too old for Extensions.loadUnpacked.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from jobfilter.storage import Store
from jobfilter.web import Api, build_server

from .cdp import CdpError, Chrome
from .conftest import FIXTURES
from .jsrunner import find_chrome

PAGES = FIXTURES / "extension"

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs Chrome/Chromium")

VIEW_URL = "https://www.linkedin.com/jobs/view/4012345678/"
SEARCH_URL = "https://www.linkedin.com/jobs/search/?currentJobId=3998877665&keywords=robotics"
FEED_URL = "https://www.linkedin.com/feed/"
INDEED_URL = "https://www.indeed.com/viewjob?jk=a1b2c3d4e5f60718"

# What the badge shows, read through its (open) shadow root.
READ_BADGE = """(() => {
  const host = document.getElementById("jobfilter-badge-host");
  if (!host) return null;
  const root = host.shadowRoot;
  const text = (sel) => [...root.querySelectorAll(sel)].map((n) => n.textContent);
  return { name: text(".pill .name")[0] || null, quote: text("blockquote")[0] || null,
           meta: text(".meta"), warn: text(".warn") };
})()"""


def settled(badge) -> bool:
    return bool(badge and badge["name"] and badge["name"] != "Checking…" and not any(
        "Waiting for the server" in w for w in badge["warn"]
    ))


@pytest.fixture(scope="module")
def chrome():
    try:
        browser = Chrome(
            Path("extension"),
            intercept=["https://www.linkedin.com/*", "https://www.indeed.com/*"],
        )
    except CdpError as exc:
        pytest.skip(f"this Chrome cannot load an unpacked extension: {exc}")
    browser.pages = {
        VIEW_URL: (PAGES / "linkedin_job_view.html").read_bytes(),
        SEARCH_URL: (PAGES / "linkedin_search_pane.html").read_bytes(),
        FEED_URL: (PAGES / "linkedin_feed.html").read_bytes(),
        INDEED_URL: (PAGES / "indeed_viewjob.html").read_bytes(),
    }
    yield browser
    browser.close()


@pytest.fixture
def tab(chrome: Chrome):
    tab = chrome.open_tab()
    yield tab
    tab.close()


@pytest.fixture
def worker(chrome: Chrome) -> str:
    return chrome.service_worker()


@pytest.fixture
def settings(chrome: Chrome, worker: str):
    """Set extension settings for one test; restore defaults afterwards."""

    def apply(values: dict) -> None:
        chrome.evaluate(worker, f"chrome.storage.sync.set({json.dumps(values)})")

    yield apply
    chrome.evaluate(worker, "chrome.storage.sync.clear()")


def test_linkedin_posting_gets_a_badge(tab) -> None:
    tab.goto(VIEW_URL)
    badge = tab.wait_for(READ_BADGE, settled)
    assert badge["name"] == "U.S. persons only (export control)"
    assert badge["quote"] == "Due to export control requirements, applicants must be U.S. persons."
    assert "classifier: rules (local)" in badge["meta"]


def test_toolbar_badge_is_set(chrome: Chrome, tab, worker: str) -> None:
    tab.goto(VIEW_URL)
    tab.wait_for(READ_BADGE, settled)
    texts = chrome.evaluate(
        worker,
        """(async () => {
          const tabs = await chrome.tabs.query({});
          return Promise.all(tabs.map((t) => chrome.action.getBadgeText({ tabId: t.id })));
        })()""",
    )
    assert "USP" in texts


def test_switching_jobs_in_place_updates_the_badge(tab) -> None:
    """LinkedIn swaps the description without a page load; so does this test."""
    tab.goto(SEARCH_URL)
    assert tab.wait_for(READ_BADGE, settled)["name"] == "Security clearance required"

    tab.evaluate(
        """history.pushState({}, "", "/jobs/search/?currentJobId=3998877001&keywords=robotics");
        document.querySelector("#job-details span").innerHTML =
          "<p>Perception role in Pittsburgh.</p><p>Applicants must be a US citizen.</p>";"""
    )
    badge = tab.wait_for(
        READ_BADGE, lambda b: settled(b) and b["name"] != "Security clearance required"
    )
    assert badge["name"] == "U.S. citizens only"
    assert badge["quote"] == "Applicants must be a US citizen."


def test_a_non_job_page_gets_no_badge(tab) -> None:
    tab.goto(FEED_URL)
    tab.chrome.pump(1.5)  # long enough for the content script and its debounce
    assert tab.evaluate(READ_BADGE) is None


@pytest.fixture
def server():
    """A real jobfilter server on a free port, with an in-memory database."""
    with Store(":memory:") as store:
        api = Api(store, checker=lambda urls, expand=False: [])
        httpd = build_server(api, host="127.0.0.1", port=0)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}", store
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def test_server_mode_records_the_posting(tab, settings, server) -> None:
    base, store = server
    settings({"mode": "server", "record": True, "serverUrl": base})

    tab.goto(INDEED_URL)
    badge = tab.wait_for(
        READ_BADGE, lambda b: settled(b) and any("saved" in m for m in b["meta"])
    )
    assert badge["name"] == "U.S. citizens only"
    assert "classifier: rules (server)" in badge["meta"]

    [row] = store.postings()
    assert row["platform"] == "indeed"
    assert row["job_id"] == "a1b2c3d4e5f60718"
    assert row["company_name"] == "Saguaro Systems"
    assert row["origin"] == "browsed"
    assert row["label"] == "RESTRICTED_CITIZEN"


def test_server_down_falls_back_to_local(tab, settings) -> None:
    # Port 9 (discard) has nothing listening on a normal machine.
    settings({"mode": "server", "serverUrl": "http://127.0.0.1:9"})
    tab.goto(VIEW_URL)
    badge = tab.wait_for(READ_BADGE, lambda b: settled(b) and b["warn"])
    assert badge["name"] == "U.S. persons only (export control)"
    assert "classifier: rules (local)" in badge["meta"]
    assert any(w.startswith("Server:") for w in badge["warn"])
