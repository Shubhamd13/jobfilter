"""The sidebar's pages and the Inbox, in a real (headless) Chrome.

Same harness as test_web_ui.py. Skipped when Chrome is missing.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from jobfilter.roles import Profile, RoleRules, read_store, screen_store
from jobfilter.storage import Store
from jobfilter.web import Api, build_server

from .cdp import Chrome, Tab
from .jsrunner import find_chrome
from .test_applications import JOBS, seed
from .test_reader import PROFILE_WITH_FIT, FakeReader
from .test_storage import make_posting, make_result
from .test_web_ui import RANGE, chrome, load  # noqa: F401 - chrome is a fixture
from .test_web_ui_marks import served  # noqa: F401 - a fixture

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs Chrome/Chromium")

VISIBLE = "[...document.querySelectorAll('.view')].filter(v => !v.hidden).map(v => v.dataset.view)"
CURRENT = "[...document.querySelectorAll('.nav-link[aria-current]')].map(a => a.dataset.view)"
LIST = "[...document.querySelectorAll('#inbox-list .inbox-item')].map(b => b.querySelector('.inbox-title').textContent)"
SELECTED = "document.querySelector('#inbox-list .inbox-item[aria-selected=\"true\"] .inbox-title')?.textContent || ''"
READING = "document.querySelector('#reader h3')?.textContent || ''"
TABS = (
    "Object.fromEntries([...document.querySelectorAll('#inbox-tabs .tab')]"
    ".map(t => [t.dataset.tab, t.querySelector('.count').textContent]))"
)


@pytest.fixture
def page(chrome: Chrome, served: tuple[str, Store, dict[str, int]]) -> Iterator[Tab]:  # noqa: F811
    tab = chrome.open_tab()
    load(tab, served[0])
    tab.wait_for(READING, bool, timeout=5)
    yield tab
    tab.close()


def key(tab: Tab, name: str) -> None:
    tab.evaluate(
        f"document.dispatchEvent(new KeyboardEvent('keydown', {{key: '{name}', bubbles: true}}))"
    )


def go(tab: Tab, view: str) -> None:
    tab.evaluate(f"location.hash = '#{view}'")
    tab.wait_for(VISIBLE, lambda visible: visible == [view], timeout=5)


def reload(tab: Tab) -> None:
    """Reload in place. A new hash alone does not reload the document, so `load` cannot."""
    tab.evaluate("window.__stale = true; location.reload()")
    tab.wait_for(f"!window.__stale && ({RANGE})", lambda text: bool(text) and " of " in text)


# ---- pages -------------------------------------------------------------------

def test_the_inbox_is_the_home_page(page: Tab) -> None:
    assert page.evaluate(VISIBLE) == ["inbox"]
    assert page.evaluate(CURRENT) == ["inbox"]


@pytest.mark.parametrize("view,landmark", [
    ("applications", "#apps-rows"),
    ("explore", "#rows"),
    ("companies", "#company-rows"),
    ("add", "#urls"),
])
def test_each_page_shows_alone(page: Tab, view: str, landmark: str) -> None:
    go(page, view)
    assert page.evaluate(CURRENT) == [view]
    assert page.evaluate(f"document.querySelector('{landmark}').closest('.view').dataset.view") == view
    assert page.evaluate(f"document.querySelector('{landmark}').offsetParent !== null"), "its content is on screen"
    assert page.evaluate("document.querySelector('#inbox-list').offsetParent === null"), "and the inbox is not"


def test_the_page_survives_a_reload(page: Tab) -> None:
    go(page, "companies")
    reload(page)
    assert page.evaluate(VISIBLE) == ["companies"]
    assert page.evaluate(CURRENT) == ["companies"]

    page.evaluate("location.hash = '#no-such-page'")
    reload(page)
    assert page.evaluate(VISIBLE) == ["inbox"], "an unknown hash falls back to home"


def test_changing_page_keeps_the_pager_buttons(page: Tab, served) -> None:
    """The page sections once shared the pager buttons' class, and hid them."""
    _, store, _ = served
    store.record_run(
        "t", [make_result(make_posting(job_id=f"extra{i}", title="Engineer")) for i in range(120)]
    )
    buttons = "[...document.querySelectorAll('#pager-top .pages button')].map(b => b.offsetParent !== null)"

    go(page, "explore")
    reload(page)
    assert page.evaluate(buttons) == [True] * 4, "previous, 1, 2, next"

    go(page, "inbox")
    go(page, "explore")
    assert page.evaluate(buttons) == [True] * 4


def test_the_sidebar_counts(page: Tab) -> None:
    counts = page.evaluate(
        "Object.fromEntries(['inbox', 'explore', 'companies']"
        ".map(v => [v, document.querySelector(`#nav-${v}-count`).textContent]))"
    )
    assert counts == {"inbox": "2", "explore": str(len(JOBS)), "companies": "1"}


# ---- inbox -------------------------------------------------------------------

def test_the_inbox_lists_what_passed_and_reads_the_first(page: Tab) -> None:
    titles = page.evaluate(LIST)
    assert set(titles) == {"Robotics Software Engineer", "Senior Autonomy Engineer"}
    assert page.evaluate(TABS) == {"none": "2", "maybe": "0", "applied": "0", "skipped": "0"}

    assert page.evaluate(SELECTED) == titles[0]
    assert page.evaluate(READING) == titles[0]
    text = page.evaluate("document.querySelector('#reader .reader-text').textContent")
    assert "motion planning software" in text, "the description is in the reader"
    assert "Passed the rules" in page.evaluate("document.querySelector('#reader .reader-side').textContent")


def test_selecting_a_posting_reads_it(page: Tab) -> None:
    titles = page.evaluate(LIST)
    page.evaluate("document.querySelectorAll('#inbox-list .inbox-item')[1].click()")
    page.wait_for(READING, lambda title: title == titles[1], timeout=5)
    assert page.evaluate(SELECTED) == titles[1]


def test_keys_move_and_mark(page: Tab, served) -> None:
    _, store, ids = served
    by_title = {JOBS[job][0]: ids[job] for job in ("swe", "senior")}
    first, second = page.evaluate(LIST)

    key(page, "j")
    page.wait_for(READING, lambda title: title == second, timeout=5)
    key(page, "k")
    page.wait_for(READING, lambda title: title == first, timeout=5)

    # Marking takes it off "To review" and moves on to the next posting.
    key(page, "s")
    page.wait_for(LIST, lambda titles: titles == [second], timeout=5)
    page.wait_for(READING, lambda title: title == second, timeout=5)
    assert store.posting(by_title[first])["app_status"] == "skipped"
    assert page.evaluate(TABS) == {"none": "1", "maybe": "0", "applied": "0", "skipped": "1"}

    key(page, "a")
    page.wait_for(LIST, lambda titles: titles == [], timeout=5)
    assert store.posting(by_title[second])["app_status"] == "applied"
    assert "Nothing left to review" in page.evaluate("document.querySelector('#inbox-list').textContent")
    assert "Select a posting" in page.evaluate("document.querySelector('#reader').textContent")


def test_keys_are_left_alone_while_typing(page: Tab, served) -> None:
    _, store, ids = served
    page.evaluate(
        "document.querySelector('#q').dispatchEvent("
        "new KeyboardEvent('keydown', {key: 's', bubbles: true}))"
    )
    page.wait_for("1", lambda _: False, timeout=0.6)
    assert all(store.posting(i)["app_status"] is None for i in ids.values())


def test_the_reader_marks_and_the_tabs_follow(page: Tab, served) -> None:
    _, store, ids = served
    first = page.evaluate(READING)
    page.evaluate("document.querySelector('#reader button.mark[data-status=\"maybe\"]').click()")
    page.wait_for(TABS, lambda tabs: tabs["maybe"] == "1", timeout=5)
    assert first not in page.evaluate(LIST)

    page.evaluate("document.querySelector('#inbox-tabs .tab[data-tab=\"maybe\"]').click()")
    page.wait_for(LIST, lambda titles: titles == [first], timeout=5)
    page.wait_for(READING, lambda title: title == first, timeout=5)

    # A note is saved from the reader and the posting stays where it is.
    page.evaluate("document.querySelector('#reader .mine-note').value = 'reach out to the team lead'")
    page.evaluate("document.querySelector('#reader .mine-note-save').click()")
    marked = next(i for i in ids.values() if store.posting(i)["app_status"] == "maybe")
    page.wait_for("1", lambda _: store.posting(marked)["app_note"] == "reach out to the team lead", timeout=5)
    assert page.evaluate(LIST) == [first]


def test_applied_lists_the_latest_application_first(page: Tab, served, monkeypatch: pytest.MonkeyPatch) -> None:
    _, store, ids = served
    by_title = {JOBS[job][0]: ids[job] for job in ("swe", "senior")}
    first, second = page.evaluate(LIST)

    clock = iter(["2026-10-01T09:00:00+00:00", "2026-10-02T09:00:00+00:00"])
    monkeypatch.setattr("jobfilter.storage.store.utcnow", lambda: next(clock))
    store.set_application(by_title[first], "applied")
    store.set_application(by_title[second], "applied")

    page.evaluate("document.querySelector('#inbox-tabs .tab[data-tab=\"applied\"]').click()")
    page.wait_for(LIST, lambda titles: titles == [second, first], timeout=5)
    assert " · applied " in page.evaluate("document.querySelector('#inbox-list .inbox-meta').textContent")


def test_a_mark_made_on_explore_reaches_the_inbox(page: Tab, served) -> None:
    _, _, ids = served
    go(page, "explore")
    page.evaluate(
        f"document.querySelector('#rows button.mark[data-id=\"{ids['swe']}\"][data-status=\"applied\"]').click()"
    )
    page.wait_for("document.querySelectorAll('#rows button.mark.on').length", lambda n: n == 1, timeout=5)

    go(page, "inbox")
    page.wait_for(LIST, lambda titles: titles == ["Senior Autonomy Engineer"], timeout=5)
    assert page.evaluate(TABS)["applied"] == "1"


# ---- companies follow the Explore filters -----------------------------------------

COMPANY_TOTALS = (
    "Object.fromEntries([...document.querySelectorAll('#company-rows tr')]"
    ".map(tr => [tr.children[0].textContent, tr.children[2].textContent]))"
)


def test_explore_filters_narrow_the_company_counts(page: Tab) -> None:
    go(page, "companies")
    page.wait_for(COMPANY_TOTALS, lambda totals: totals == {"acme": str(len(JOBS))}, timeout=5)
    assert page.evaluate("document.querySelector('#company-filter-note').hidden") is True

    # Tick "Passed the rules" on Explore: two of the seven postings.
    page.evaluate("[...document.querySelectorAll('#stage-list input')].find(b => b.value === 'model').click()")
    page.wait_for(COMPANY_TOTALS, lambda totals: totals == {"acme": "2"}, timeout=5)
    note = page.evaluate("document.querySelector('#company-filter-note').textContent")
    assert page.evaluate("document.querySelector('#company-filter-note').hidden") is False
    assert "2 of 7 jobs" in " ".join(note.split())

    page.evaluate("document.querySelector('#company-filter-clear').click()")
    page.wait_for(COMPANY_TOTALS, lambda totals: totals == {"acme": str(len(JOBS))}, timeout=5)
    assert page.evaluate("document.querySelector('#company-filter-note').hidden") is True


# ---- model verdicts -----------------------------------------------------------------

@pytest.fixture
def read_page(chrome: Chrome) -> Iterator[Tab]:  # noqa: F811
    """A database a model has read: the plain role is a fit, the senior one a skip."""
    profile = Profile.from_dict(PROFILE_WITH_FIT)
    with Store(":memory:") as store:
        seed(store)
        read_store(store, FakeReader(), rules=RoleRules.load(), profile=profile)
        api = Api(
            store,
            lambda urls, expand=False: [],
            screener=lambda s: screen_store(s, RoleRules.load(), profile),
        )
        server = build_server(api, port=0)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        tab = chrome.open_tab()
        try:
            load(tab, f"http://127.0.0.1:{server.server_address[1]}/")
            tab.wait_for(READING, bool, timeout=5)
            yield tab
        finally:
            tab.close()
            server.shutdown()
            server.server_close()


def test_the_inbox_leaves_out_what_the_model_skipped(read_page: Tab) -> None:
    assert read_page.evaluate(LIST) == ["Robotics Software Engineer"]
    item = read_page.evaluate("document.querySelector('#inbox-list .inbox-item').textContent")
    assert "Fit" in item and "Robotics software, 2 years" in item

    summary = read_page.evaluate("document.querySelector('#inbox-verdicts').textContent")
    assert "Fit 1" in summary and "1 skipped by the model" in summary
    assert read_page.evaluate("document.querySelector('#nav-inbox-count').textContent") == "1"


def test_the_reader_shows_what_the_model_read(read_page: Tab) -> None:
    # textContent runs a term into its value, so compare with the spaces out.
    side = "".join(read_page.evaluate("document.querySelector('#reader .reader-side').textContent").split())
    assert "ModelverdictFitRoboticssoftware,2years" in side
    assert "Yearsasked2+" in side and "Readbyfake:1" in side


def test_explore_filters_by_verdict(read_page: Tab) -> None:
    go(read_page, "explore")
    counts = read_page.evaluate(
        "Object.fromEntries([...document.querySelectorAll('#verdict-list .check-option')]"
        ".map(o => [o.querySelector('input').value, o.querySelector('.count').textContent]))"
    )
    assert counts == {"best": "0", "fit": "1", "stretch": "0", "unknown": "0", "unread": "5", "skip": "1"}

    read_page.evaluate("[...document.querySelectorAll('#verdict-list input')].find(b => b.value === 'skip').click()")
    titles = "[...document.querySelectorAll('#rows tr.clickable td.title')].map(td => td.textContent.trim())"
    read_page.wait_for(titles, lambda found: len(found) == 1, timeout=5)
    assert read_page.evaluate(titles)[0].startswith("Senior Autonomy Engineer")
    assert "Skip" in read_page.evaluate(titles)[0], "the verdict shows beside the title"


# ---- new postings: the screen-and-read bar ------------------------------------------

BAR = "document.querySelector('#process-bar').hidden ? '' : document.querySelector('#process-text').textContent"


@pytest.fixture
def fresh_page(chrome: Chrome) -> Iterator[tuple[Tab, Store]]:  # noqa: F811
    """Seven postings just added: labelled, but not screened or read."""
    profile = Profile.from_dict(PROFILE_WITH_FIT)
    with Store(":memory:") as store:
        seed(store)
        api = Api(
            store,
            lambda urls, expand=False: [],
            screener=lambda s: screen_store(s, RoleRules.load(), profile),
            reader=lambda s, progress: read_store(
                s, FakeReader(), rules=RoleRules.load(), profile=profile, progress=progress
            ),
        )
        server = build_server(api, port=0)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        tab = chrome.open_tab()
        try:
            load(tab, f"http://127.0.0.1:{server.server_address[1]}/")
            yield tab, store
        finally:
            tab.close()
            server.shutdown()
            server.server_close()


def test_new_postings_offer_to_be_screened_and_read(fresh_page: tuple[Tab, Store]) -> None:
    tab, store = fresh_page
    tab.wait_for(BAR, bool, timeout=5)
    assert "7 postings not screened yet" in tab.evaluate(BAR)
    assert tab.evaluate("document.querySelector('#process-run').hidden") is False
    assert tab.evaluate(LIST) == [], "nothing has passed the rules yet"

    tab.evaluate("document.querySelector('#process-run').click()")
    tab.wait_for(LIST, lambda titles: titles == ["Robotics Software Engineer"], timeout=10)

    assert store.count_postings(stage=["unscreened"]) == 0
    assert "The model read 2 postings" in tab.evaluate(BAR)
    assert tab.evaluate("document.querySelector('#process-run').hidden") is True, "nothing is left waiting"
    assert "Fit" in tab.evaluate("document.querySelector('#inbox-list .inbox-item').textContent")


def test_nothing_waiting_shows_no_bar(read_page: Tab) -> None:
    read_page.wait_for("1", lambda _: False, timeout=0.6)
    assert read_page.evaluate(BAR) == ""


# ---- best fit ---------------------------------------------------------------------

def test_best_fits_lead_the_inbox_with_the_skills_they_name(chrome: Chrome) -> None:  # noqa: F811
    from .test_match import NAV_JD, PLAIN_JD, PROFILE as SKILLED, add

    profile = Profile.from_dict(SKILLED)
    with Store(":memory:") as store:
        add(store, "plain", "Robotics Software Engineer", PLAIN_JD)
        add(store, "nav", "Navigation Software Engineer", NAV_JD)
        add(store, "some", "Autonomy Software Engineer", PLAIN_JD + "\nWe use simulation.")
        read_store(store, FakeReader(), rules=RoleRules.load(), profile=profile)
        server = build_server(Api(store, lambda urls, expand=False: []), port=0)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        tab = chrome.open_tab()
        try:
            load(tab, f"http://127.0.0.1:{server.server_address[1]}/")
            tab.wait_for(READING, bool, timeout=5)

            # Best fit first; then fits by how many of your skills they name (4, then 2).
            assert tab.evaluate(LIST) == [
                "Navigation Software Engineer", "Autonomy Software Engineer", "Robotics Software Engineer",
            ]
            first = tab.evaluate("document.querySelector('#inbox-list .inbox-item').textContent")
            assert "Best fit" in first and "ROS 2 · Nav2 · C++" in first
            assert "Best fit 1" in tab.evaluate("document.querySelector('#inbox-verdicts').textContent")

            side = "".join(tab.evaluate("document.querySelector('#reader .reader-side').textContent").split())
            assert "YourskillsROS2Nav2C++matchscore7" in side
        finally:
            tab.close()
            server.shutdown()
            server.server_close()

