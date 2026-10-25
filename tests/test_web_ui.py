"""The web page itself, in a real (headless) Chrome against a real server.

test_web.py covers the API and that the static files are served; these run
the page's JavaScript. They exist because the filter checkboxes once broke in
a way no API test could see: after a reload restored saved filters, clicking
a region checkbox changed a Set the page no longer read, so the box "unchecked
itself" on the next render.

Skipped when Chrome is missing.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.fetchers import HttpClient
from jobfilter.models import Label, Platform, Result
from jobfilter.storage import Store
from jobfilter.web import Api, build_server, make_refresher

from .cdp import CdpError, Chrome, Tab
from .conftest import LEVER_SLUG, load_fixture
from .jsrunner import find_chrome
from .test_refresh import LEVER_BOARD_URL, Boards, seed_board
from .test_storage import make_posting, make_result

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs Chrome/Chromium")

# job id -> (location, label)
POSTINGS = {
    "sf": ("San Francisco Bay Area", Label.OPEN),
    "austin": ("Austin, Texas, United States", Label.RESTRICTED_CITIZEN),
    "london": ("London", Label.OPEN),
    "abu": ("Abu Dhabi", Label.UNCLEAR),
    "remote": ("Remote", Label.OPEN),
}

RANGE = "document.querySelector('#pager-top .range')?.textContent || ''"


@pytest.fixture(scope="module")
def chrome():
    # The harness loads an extension on start; the page tests do not use it.
    try:
        browser = Chrome(Path("extension"), intercept=[])
    except CdpError as exc:
        pytest.skip(f"Chrome would not start under DevTools: {exc}")
    yield browser
    browser.close()


@contextmanager
def serve(results: list[Result]) -> Iterator[str]:
    """A server seeded with `results` on a fresh port. A fresh origin means empty localStorage."""
    with Store(":memory:") as store:
        store.record_run("t", results)
        server = build_server(Api(store, lambda urls, expand=False: []), port=0)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/"
        finally:
            server.shutdown()
            server.server_close()


@pytest.fixture
def site():
    with serve(
        [
            make_result(
                dataclasses.replace(make_posting(job_id=job_id), location=location),
                label=label,
            )
            for job_id, (location, label) in POSTINGS.items()
        ]
    ) as url:
        yield url


@pytest.fixture
def page(chrome: Chrome, site: str):
    tab = chrome.open_tab()
    load(tab, site)
    yield tab
    tab.close()


def load(tab: Tab, url: str) -> None:
    """Navigate and wait until the *new* document has rendered its pager."""
    tab.evaluate("window.__stale = true")
    tab.goto(url)
    tab.wait_for(f"!window.__stale && ({RANGE})", lambda text: bool(text) and " of " in text)


def total(tab: Tab) -> int:
    return int(tab.evaluate(RANGE).rsplit(" of ", 1)[-1].replace(",", "") or 0)


def checked(tab: Tab, list_id: str) -> list[str]:
    return tab.evaluate(
        f"[...document.querySelectorAll('#{list_id} input:checked')].map(b => b.value).sort()"
    )


def click(tab: Tab, list_id: str, value: str, expect_total: int) -> None:
    """Click a checkbox like a user, then wait for the table to show the result."""
    tab.evaluate(
        f"[...document.querySelectorAll('#{list_id} input')]"
        f".find(b => b.value === '{value}').click()"
    )
    tab.wait_for(RANGE, lambda _: total(tab) == expect_total, timeout=5)


@pytest.mark.parametrize("reload_first", [False, True], ids=["fresh", "after-reload"])
def test_region_checkboxes_toggle(page: Tab, site: str, reload_first: bool) -> None:
    if reload_first:
        # Anything saved -- here, a region ticked and unticked -- used to break them.
        click(page, "region-list", "us", expect_total=2)
        load(page, site)
        assert checked(page, "region-list") == ["us"], "saved region restored"
        click(page, "region-list", "us", expect_total=5)

    click(page, "region-list", "outside_us", expect_total=2)
    assert checked(page, "region-list") == ["outside_us"]

    click(page, "region-list", "unknown", expect_total=3)
    assert checked(page, "region-list") == ["outside_us", "unknown"]

    click(page, "region-list", "outside_us", expect_total=1)
    assert checked(page, "region-list") == ["unknown"]

    click(page, "region-list", "unknown", expect_total=5)
    assert checked(page, "region-list") == []


@pytest.mark.parametrize("reload_first", [False, True], ids=["fresh", "after-reload"])
def test_label_checkboxes_toggle(page: Tab, site: str, reload_first: bool) -> None:
    if reload_first:
        click(page, "region-list", "us", expect_total=2)
        click(page, "region-list", "us", expect_total=5)
        load(page, site)

    click(page, "label-list", "OPEN", expect_total=3)
    click(page, "label-list", "UNCLEAR", expect_total=4)
    assert checked(page, "label-list") == ["OPEN", "UNCLEAR"]

    click(page, "label-list", "OPEN", expect_total=1)
    assert checked(page, "label-list") == ["UNCLEAR"]


def test_checkbox_counts_follow_the_other_filters(page: Tab) -> None:
    click(page, "region-list", "us", expect_total=2)
    counts = page.evaluate(
        "Object.fromEntries([...document.querySelectorAll('#label-list .check-option')]"
        ".map(o => [o.querySelector('input').value, o.querySelector('.count').textContent]))"
    )
    assert counts["OPEN"] == "1"
    assert counts["RESTRICTED_CITIZEN"] == "1"
    assert counts["UNCLEAR"] == "0"


# ---- the By company table's sort ---------------------------------------------

# company -> (platform, labels of its postings)
COMPANIES = {
    "acme": (Platform.LEVER, [Label.OPEN] * 3),
    "beta": (Platform.GREENHOUSE, [Label.OPEN, Label.RESTRICTED_CITIZEN]),
    "gamma": (Platform.ASHBY, [Label.OPEN] * 2),
    "delta": (Platform.LEVER, [Label.OPEN]),
}

COMPANY_ORDER = "[...document.querySelectorAll('#company-rows tr td:first-child')].map(td => td.textContent)"
SORT_MARKS = (
    "Object.fromEntries([...document.querySelectorAll('#company-head th[data-key]')]"
    ".filter(th => th.dataset.dir).map(th => [th.dataset.key, th.querySelector('.sort-mark').textContent]))"
)


@pytest.fixture
def company_page(chrome: Chrome) -> Iterator[tuple[Tab, str]]:
    results = [
        make_result(make_posting(slug=slug, job_id=f"{slug}{i}", platform=platform), label=label)
        for slug, (platform, labels) in COMPANIES.items()
        for i, label in enumerate(labels)
    ]
    with serve(results) as url:
        tab = chrome.open_tab()
        load(tab, url)
        tab.wait_for(COMPANY_ORDER, lambda rows: len(rows) == 4)
        yield tab, url
        tab.close()


def sort_by(tab: Tab, key: str, *, shift: bool = False) -> None:
    tab.evaluate(
        f"document.querySelector('#company-head th[data-key={key}] button')"
        f".dispatchEvent(new MouseEvent('click', {{bubbles: true, shiftKey: {str(shift).lower()}}}))"
    )


def test_company_table_sorts_by_several_columns(company_page: tuple[Tab, str]) -> None:
    tab, url = company_page
    # The server's order, shown as such: most restricted, then most postings.
    assert tab.evaluate(COMPANY_ORDER) == ["beta", "acme", "gamma", "delta"]
    assert tab.evaluate(SORT_MARKS) == {"restricted": "▼1", "total": "▼2"}
    assert tab.evaluate("document.getElementById('company-sort-reset').hidden")

    # A click sorts by that column alone; text starts ascending.
    sort_by(tab, "platform")
    assert tab.evaluate(COMPANY_ORDER) == ["gamma", "beta", "acme", "delta"]
    assert tab.evaluate(SORT_MARKS) == {"platform": "▲"}

    # Shift-click adds a tie-breaker; numbers start descending.
    sort_by(tab, "open_count", shift=True)
    assert tab.evaluate(SORT_MARKS) == {"platform": "▲1", "open_count": "▼2"}
    assert tab.evaluate(COMPANY_ORDER) == ["gamma", "beta", "acme", "delta"]

    # Shift-click again flips the tie-breaker.
    sort_by(tab, "open_count", shift=True)
    assert tab.evaluate(COMPANY_ORDER) == ["gamma", "beta", "delta", "acme"]

    # Clicking the leading column flips it and keeps the tie-breaker.
    sort_by(tab, "platform")
    assert tab.evaluate(SORT_MARKS) == {"platform": "▼1", "open_count": "▲2"}
    assert tab.evaluate(COMPANY_ORDER) == ["delta", "acme", "beta", "gamma"]
    assert tab.evaluate(
        "document.querySelector('#company-head th[data-key=platform]').getAttribute('aria-sort')"
    ) == "descending"

    # A third shift-click drops the tie-breaker.
    sort_by(tab, "open_count", shift=True)
    assert tab.evaluate(SORT_MARKS) == {"platform": "▼"}

    # The sort survives a reload, and Reset puts the server's order back.
    sort_by(tab, "open_count", shift=True)
    load(tab, url)
    tab.wait_for(COMPANY_ORDER, lambda rows: len(rows) == 4)
    assert tab.evaluate(COMPANY_ORDER) == ["acme", "delta", "beta", "gamma"]
    tab.evaluate("document.getElementById('company-sort-reset').click()")
    assert tab.evaluate(COMPANY_ORDER) == ["beta", "acme", "gamma", "delta"]
    assert tab.evaluate("document.getElementById('company-sort-reset').hidden")


# --------------------------------------------------------------------------
# Refresh boards: the button, the Changes column, the change tags
# --------------------------------------------------------------------------

def company_cell(column: str) -> str:
    """Text of one cell in the saved Lever company's row of the company table."""
    return (
        "(() => { const row = [...document.querySelectorAll('#company-rows tr')]"
        f".find(tr => tr.cells[0].textContent === '{LEVER_SLUG}' && tr.cells[1].textContent === 'lever');"
        f" return row ? row.querySelector('{column}').textContent.trim() : null; }})()"
    )


CHANGES = company_cell("td.changes")
TITLE_TAGS = (
    "[...document.querySelectorAll('#rows tr.clickable')].map(tr => "
    "[...tr.querySelectorAll('.change-tag')].map(t => t.textContent).join(','))"
)


@pytest.fixture
def refresh_page(chrome: Chrome, monkeypatch) -> Iterator[tuple[Tab, Boards, list]]:
    """The real refresher over a fake Lever whose board the test can edit.

    Stored: the saved board minus its first job, and one LinkedIn posting.
    """
    clf = RuleClassifier()
    board = load_fixture("lever_board_rai.json")
    lever = Boards({LEVER_BOARD_URL: board[1:]})
    monkeypatch.setattr(
        "jobfilter.web.api.HttpClient",
        lambda timeout: HttpClient(transport=lever, backoff=0.0, sleep=lambda _: None),
    )
    with Store(":memory:") as store:
        seed_board(store, clf, lever)
        store.record_run("t", [
            make_result(make_posting(slug="seen", job_id="l1", platform=Platform.LINKEDIN))
        ])
        lever.routes[LEVER_BOARD_URL] = board
        api = Api(store, lambda urls, expand=False: [], clf, make_refresher(store, clf))
        server = build_server(api, port=0)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        tab = chrome.open_tab()
        try:
            load(tab, f"http://127.0.0.1:{server.server_address[1]}/")
            tab.wait_for(CHANGES, bool)
            yield tab, lever, board
        finally:
            tab.close()
            api.refresh_job.join(5)
            server.shutdown()
            server.server_close()


def test_refresh_boards_shows_what_changed(refresh_page: tuple[Tab, Boards, list]) -> None:
    tab, lever, board = refresh_page
    assert tab.evaluate(CHANGES) == "not refreshed yet"
    # Only the company with a board gets a refresh button; LinkedIn's does not.
    assert tab.evaluate(
        "[...document.querySelectorAll('.refresh-one')].map(b => b.dataset.platform)"
    ) == ["lever"]

    # Refresh everything: the board gained one job since it was stored.
    tab.evaluate("document.getElementById('refresh-all').click()")
    assert tab.wait_for(CHANGES, lambda text: text == "+1 new") == "+1 new"
    status = tab.evaluate("document.getElementById('refresh-status').textContent")
    assert "Refreshed 1 board" in status and "1 changed (+1 new)" in status
    assert tab.evaluate("document.getElementById('refresh-all').disabled") is False
    assert total(tab) == len(board) + 1  # the new job is in the table (plus LinkedIn's)

    # Clicking the change count shows just those postings, tagged.
    tab.evaluate("document.querySelector('.changes-link').click()")
    tab.wait_for(RANGE, lambda _: total(tab) == 1, timeout=5)
    assert tab.evaluate(TITLE_TAGS) == ["new"]
    assert tab.evaluate("document.getElementById('changed-only').checked") is True

    # The board drops a job; refresh just this company from its row.
    lever.routes[LEVER_BOARD_URL] = board[:-1]
    tab.evaluate("document.querySelector('.refresh-one').click()")
    assert tab.wait_for(CHANGES, lambda text: text == "1 closed") == "1 closed"
    # The filter still shows that company's latest changes: now the closed job.
    tab.wait_for(TITLE_TAGS, lambda tags: tags == ["closed"], timeout=5)
    assert tab.evaluate(TITLE_TAGS) == ["closed"]

    # Reset: every posting again, the closed one kept and still tagged.
    tab.evaluate("document.getElementById('reset').click()")
    tab.wait_for(RANGE, lambda _: total(tab) == len(board) + 1, timeout=5)
    assert sorted(tab.evaluate(TITLE_TAGS)).count("closed") == 1
    assert tab.evaluate("document.getElementById('changed-only').checked") is False


TITLES = "[...document.querySelectorAll('#rows td.title')].map(td => td.firstChild.textContent.trim())"


def test_the_posted_header_cycles_newest_oldest_then_last_checked(chrome: Chrome) -> None:
    from datetime import date, timedelta

    def posted(days: int | None) -> dict:
        if days is None:
            return {}
        return {"createdAt": f"{(date.today() - timedelta(days=days)).isoformat()}T12:00:00"}

    ages = {"mid": 10, "old": 40, "new": 1, "undated": None}
    results = [make_result(make_posting(job_id=name, title=name, raw=posted(days))) for name, days in ages.items()]
    with serve(results) as url:
        tab = chrome.open_tab()
        try:
            load(tab, url)
            checked_order = tab.evaluate(TITLES)
            assert checked_order == ["undated", "new", "old", "mid"], "last checked first"

            def click_posted(expected: list[str], mark: str, sort: str) -> None:
                tab.evaluate("document.querySelector('#posted-head button').click()")
                tab.wait_for(TITLES, lambda titles: titles == expected, timeout=5)
                assert tab.evaluate("document.querySelector('#posted-head .sort-mark').textContent") == mark
                assert tab.evaluate("document.querySelector('#pager-top [data-role=sort]').value") == sort

            click_posted(["new", "mid", "old", "undated"], "▼", "posted")
            click_posted(["old", "mid", "new", "undated"], "▲", "posted_asc")
            click_posted(checked_order, "", "newest")
        finally:
            tab.close()


def test_the_calendar_counts_today_and_lists_its_jobs(chrome: Chrome) -> None:
    results = [make_result(make_posting(job_id=name, title=name)) for name in ("first", "second", "third")]
    with Store(":memory:") as store:
        store.record_run("t", results)
        ids = {row["job_id"]: row["posting_id"] for row in store.postings()}
        store.set_application(ids["second"], "applied")
        server = build_server(Api(store, lambda urls, expand=False: []), port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        tab = chrome.open_tab()
        try:
            tab.goto(f"http://127.0.0.1:{server.server_address[1]}/#calendar")
            counts = "[...document.querySelectorAll('#cal-grid .cal-count')].map(c => c.textContent)"
            tab.wait_for(counts, lambda shown: shown == ["1"], timeout=5)
            assert tab.evaluate("document.querySelector('#cal-grid .cal-cell.has').classList.contains('today')")

            tab.evaluate("document.querySelector('#cal-grid .cal-cell.has').click()")
            jobs = "[...document.querySelectorAll('#cal-day-rows .cal-job-title')].map(t => t.textContent)"
            tab.wait_for(jobs, lambda shown: shown == ["second"], timeout=5)

            # Added counts every posting first seen today, applied or not.
            tab.evaluate("document.querySelector('#cal-kinds [data-kind=added]').click()")
            tab.evaluate("document.querySelector('#cal-sets [data-set=explore]').click()")
            tab.wait_for(counts, lambda shown: shown == ["3"], timeout=5)
        finally:
            tab.close()
            server.shutdown()
            server.server_close()
