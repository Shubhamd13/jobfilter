"""The page's step buttons (OA / Interview / Offer / Rejected), in a real (headless) Chrome.

Same harness as test_web_ui.py. Skipped when Chrome is missing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from jobfilter.storage import Store

from .cdp import Chrome, Tab
from .jsrunner import find_chrome
from .test_web_ui import chrome, load  # noqa: F401 - chrome is a fixture
from .test_web_ui_marks import press, served  # noqa: F401 - served is a fixture

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs Chrome/Chromium")

STEPS = "[...document.querySelectorAll('#d-body button.step')].map(b => b.dataset.stage)"
REACHED = "[...document.querySelectorAll('#d-body button.step.on')].map(b => b.dataset.stage)"


@pytest.fixture
def page(chrome: Chrome, served: tuple[str, Store, dict[str, int]]) -> Iterator[Tab]:  # noqa: F811
    tab = chrome.open_tab()
    load(tab, served[0])
    # Record what the page asked, and say yes or no for the person.
    tab.evaluate("window.__asked = []; window.__answer = true;"
                 "window.confirm = (text) => { window.__asked.push(text); return window.__answer; }")
    yield tab
    tab.close()


def open_dialog(tab: Tab, posting_id: int) -> None:
    tab.evaluate(f"document.querySelector('#rows tr[data-id=\"{posting_id}\"] td.title').click()")
    tab.wait_for("document.querySelector('#detail').open", bool, timeout=5)


def step(tab: Tab, stage: str) -> None:
    tab.evaluate(f"document.querySelector('#d-body button.step[data-stage=\"{stage}\"]').click()")


def test_steps_show_once_a_posting_is_applied(page: Tab, served) -> None:
    _, _, ids = served
    open_dialog(page, ids["swe"])
    assert page.evaluate(STEPS) == [], "nothing to record before you applied"

    press(page, ids["swe"], "applied", within="#d-body")
    page.wait_for(STEPS, lambda found: found == ["oa", "interview", "offer", "rejected"], timeout=5)
    assert page.evaluate(REACHED) == []
    assert "waiting to hear back" in page.evaluate("document.querySelector('#d-body .mine-box').textContent")


def test_a_step_is_stored_dated_and_taken_back(page: Tab, served) -> None:
    _, store, ids = served
    store.set_application(ids["swe"], "applied")
    open_dialog(page, ids["swe"])

    step(page, "oa")
    page.wait_for(REACHED, lambda found: found == ["oa"], timeout=5)
    step(page, "rejected")
    page.wait_for(REACHED, lambda found: found == ["oa", "rejected"], timeout=5)
    assert store.posting(ids["swe"])["outcome"] == "rejected"
    assert page.evaluate("document.querySelector('#d-body button.step.on .step-when').textContent") == "just now"
    assert "waiting to hear back" not in page.evaluate("document.querySelector('#d-body .mine-box').textContent")
    assert store.posting(ids["swe"])["app_status"] == "applied", "a step is not a mark"

    step(page, "rejected")
    page.wait_for(REACHED, lambda found: found == ["oa"], timeout=5)
    assert store.posting(ids["swe"])["outcome"] == "oa"


def test_clearing_an_applied_mark_with_steps_asks_first(page: Tab, served) -> None:
    url, store, ids = served
    store.set_application(ids["swe"], "applied")
    store.set_outcome(ids["swe"], "interview")
    store.set_application(ids["senior"], "applied")
    load(page, url)
    page.evaluate("window.__asked = []; window.__answer = false;"
                  "window.confirm = (text) => { window.__asked.push(text); return window.__answer; }")

    # Said no: the mark and its step stay.
    press(page, ids["swe"], "applied")
    page.wait_for("window.__asked.length", lambda asked: asked == 1, timeout=5)
    assert store.posting(ids["swe"])["outcome"] == "interview"

    # Nothing to lose on the other one: it clears without asking.
    press(page, ids["senior"], "applied")
    page.wait_for("1", lambda _: store.posting(ids["senior"])["app_status"] is None, timeout=5)
    assert page.evaluate("window.__asked.length") == 1

    # Said yes: it is gone, steps and all.
    page.evaluate("window.__answer = true")
    press(page, ids["swe"], "applied")
    page.wait_for("1", lambda _: store.posting(ids["swe"])["app_status"] is None, timeout=5)
    store.set_application(ids["swe"], "applied")
    assert store.posting(ids["swe"])["outcome_steps"] is None
