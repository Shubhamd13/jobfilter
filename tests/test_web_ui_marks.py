"""The page's mark buttons and "For me" filter, in a real (headless) Chrome.

Same harness as test_web_ui.py. Skipped when Chrome is missing.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from jobfilter.roles import Profile, RoleRules, screen_store
from jobfilter.storage import Store
from jobfilter.web import Api, build_server

from .cdp import Chrome, Tab
from .jsrunner import find_chrome
from .test_applications import JOBS, seed
from .test_roles import PROFILE
from .test_web_ui import chrome, load, total  # noqa: F401 - chrome is a fixture

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs Chrome/Chromium")

TITLES = "[...document.querySelectorAll('#rows tr.clickable td.title')].map(td => td.textContent.trim())"
PRESSED = "[...document.querySelectorAll('#rows button.mark.on')].map(b => b.dataset.status)"


def screener(store: Store) -> dict[str, int]:
    return screen_store(store, RoleRules.load(), Profile.from_dict(PROFILE))


@pytest.fixture
def served() -> Iterator[tuple[str, Store, dict[str, int]]]:
    """A screened database behind a real server: (url, store, job id -> posting id)."""
    with Store(":memory:") as store:
        ids = seed(store)
        screener(store)
        server = build_server(Api(store, lambda urls, expand=False: [], screener=screener), port=0)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/", store, ids
        finally:
            server.shutdown()
            server.server_close()


@pytest.fixture
def page(chrome: Chrome, served: tuple[str, Store, dict[str, int]]) -> Iterator[Tab]:  # noqa: F811
    tab = chrome.open_tab()
    load(tab, served[0])
    yield tab
    tab.close()


def press(tab: Tab, posting_id: int, status: str, within: str = "#rows") -> None:
    tab.evaluate(
        f"document.querySelector('{within} button.mark[data-id=\"{posting_id}\"]"
        f"[data-status=\"{status}\"]').click()"
    )


def counts(tab: Tab, list_id: str) -> dict[str, str]:
    return tab.evaluate(
        f"Object.fromEntries([...document.querySelectorAll('#{list_id} .check-option')]"
        ".map(o => [o.querySelector('input').value, o.querySelector('.count').textContent]))"
    )


def test_marking_a_row_is_stored_and_shown(page: Tab, served) -> None:
    url, store, ids = served
    assert total(page) == len(JOBS)
    assert counts(page, "status-list") == {"none": "7", "maybe": "0", "applied": "0", "skipped": "0"}

    press(page, ids["swe"], "applied")
    page.wait_for(PRESSED, lambda pressed: pressed == ["applied"], timeout=5)
    assert store.posting(ids["swe"])["app_status"] == "applied"
    assert counts(page, "status-list")["applied"] == "1"
    assert page.evaluate("document.querySelector('#detail').open") is False, "a mark does not open the row"

    load(page, url)
    assert page.evaluate(PRESSED) == ["applied"], "the mark comes back from the server"


def test_pressing_a_set_mark_clears_it(page: Tab, served) -> None:
    _, store, ids = served
    press(page, ids["swe"], "maybe")
    page.wait_for(PRESSED, lambda pressed: pressed == ["maybe"], timeout=5)

    press(page, ids["swe"], "maybe")
    page.wait_for(PRESSED, lambda pressed: pressed == [], timeout=5)
    assert store.posting(ids["swe"])["app_status"] is None


def test_for_me_shows_what_passed_and_is_unmarked(page: Tab, served) -> None:
    url, _, ids = served
    assert counts(page, "stage-list")["model"] == "2"

    page.evaluate("document.querySelector('#for-me').click()")
    page.wait_for(TITLES, lambda titles: len(titles) == 2, timeout=5)
    assert set(page.evaluate(TITLES)) == {"Robotics Software Engineer", "Senior Autonomy Engineer"}

    # Marking one takes it off the list: it is no longer "to review".
    press(page, ids["swe"], "skipped")
    page.wait_for(TITLES, lambda titles: titles == ["Senior Autonomy Engineer"], timeout=5)

    # The choice is a standing one: it survives a reload.
    load(page, url)
    assert page.evaluate(TITLES) == ["Senior Autonomy Engineer"]

    page.evaluate("document.querySelector('#mine-clear').click()")
    page.wait_for(TITLES, lambda titles: len(titles) == len(JOBS), timeout=5)


def test_the_dialog_marks_and_keeps_a_note(page: Tab, served) -> None:
    _, store, ids = served
    posting_id = ids["mech"]
    page.evaluate(f"document.querySelector('#rows tr[data-id=\"{posting_id}\"] td.title').click()")
    page.wait_for("document.querySelector('#detail').open", bool, timeout=5)

    body = page.evaluate("document.querySelector('#d-body').textContent")
    assert "Mechanical / electrical" in body, "the dialog says where the funnel stopped it"
    assert page.evaluate("document.querySelector('#d-body .mine-note').disabled") is True

    press(page, posting_id, "maybe", within="#d-body")
    page.wait_for("document.querySelector('#d-body .mine-note')?.disabled", lambda disabled: disabled is False, timeout=5)

    page.evaluate("document.querySelector('#d-body .mine-note').value = 'ask about the software side'")
    page.evaluate("document.querySelector('#d-body .mine-note-save').click()")
    page.wait_for(
        "1", lambda _: store.posting(posting_id)["app_note"] == "ask about the software side", timeout=5
    )
    assert store.posting(posting_id)["app_status"] == "maybe"


def test_rescreen_places_new_postings(page: Tab, served) -> None:
    url, store, _ = served
    store.replace_role_screens([])  # as if every posting had just been added
    load(page, url)
    assert counts(page, "stage-list")["unscreened"] == "7"
    assert "not screened yet" in page.evaluate("document.querySelector('#screen-status').textContent")

    page.evaluate("document.querySelector('#screen-run').click()")
    page.wait_for("document.querySelector('#screen-status').textContent", lambda t: "passed the rules" in t, timeout=10)
    assert counts(page, "stage-list")["model"] == "2"
    assert counts(page, "stage-list")["unscreened"] == "0"
