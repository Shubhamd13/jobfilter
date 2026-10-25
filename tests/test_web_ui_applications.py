"""The Applications page, in a real (headless) Chrome.

Same harness as test_web_ui.py. Skipped when Chrome is missing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from jobfilter.storage import Store

from .cdp import Chrome, Tab
from .jsrunner import find_chrome
from .test_application_log import applications
from .test_applications import JOBS
from .test_web_ui import chrome, load  # noqa: F401 - chrome is a fixture
from .test_web_ui_marks import served as plain  # noqa: F401 - a fixture
from .test_web_ui_pages import go

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs Chrome/Chromium")

LOG = "[...document.querySelectorAll('#apps-rows tr.clickable td.title')].map(td => td.textContent.trim())"
TABS = (
    "Object.fromEntries([...document.querySelectorAll('#apps-tabs .tab')]"
    ".map(t => [t.dataset.tab, t.querySelector('.count').textContent]))"
)
# Resume name -> its numbers, as the table shows them.
RESUMES = (
    "Object.fromEntries([...document.querySelectorAll('#resume-rows tr.clickable')]"
    ".map(tr => [tr.querySelector('td.resume').textContent.trim(),"
    " [...tr.querySelectorAll('td.num')].map(td => td.textContent.trim())]))"
)
NAV = "document.querySelector('#nav-applications-count').textContent"


@pytest.fixture
def served(plain) -> tuple[str, Store, dict[str, int]]:  # noqa: F811
    """The screened database of test_web_ui_marks, with the applications on top."""
    url, store, _ = plain
    store.connection.execute("DELETE FROM postings")
    return url, store, applications(store)


@pytest.fixture
def page(chrome: Chrome, served: tuple[str, Store, dict[str, int]]) -> Iterator[Tab]:  # noqa: F811
    tab = chrome.open_tab()
    load(tab, served[0])
    tab.evaluate("window.confirm = () => true")
    go(tab, "applications")
    tab.wait_for(LOG, lambda titles: len(titles) == 5, timeout=5)
    yield tab
    tab.close()


def title(job: str) -> str:
    return JOBS[job][0]


def tab_click(tab: Tab, name: str) -> None:
    tab.evaluate(f"document.querySelector('#apps-tabs .tab[data-tab=\"{name}\"]').click()")


def resume_click(tab: Tab, resume: object) -> None:
    tab.evaluate(f"document.querySelector('#resume-rows tr[data-resume=\"{resume}\"] td.num').click()")


def test_the_page_lists_every_application(page: Tab, served) -> None:
    assert sorted(page.evaluate(LOG)) == sorted(title(j) for j in ("swe", "senior", "mech", "staff", "cleared"))
    assert page.evaluate(TABS) == {
        "all": "5", "waiting": "2", "oa": "0", "interview": "1", "offer": "0", "rejected": "2",
    }
    assert page.evaluate(NAV) == "5"


def test_the_sidebar_counts_applications_from_any_page(chrome: Chrome, served) -> None:  # noqa: F811
    tab = chrome.open_tab()
    load(tab, served[0])  # the Inbox
    tab.wait_for(NAV, lambda count: count == "5", timeout=5)
    tab.close()


def test_a_row_says_what_came_of_it(page: Tab, served) -> None:
    _, _, ids = served
    cells = page.evaluate(
        f"[...document.querySelectorAll('#apps-rows tr[data-id=\"{ids['swe']}\"] td')].map(td => td.textContent.replace(/\\s+/g, ' ').trim())"
    )
    assert cells[3] == "sim"
    assert cells[4].split("→") == ["OA just now", " Rejected just now"], "oldest step first"
    waiting = page.evaluate(
        f"document.querySelector('#apps-rows tr[data-id=\"{ids['cleared']}\"] td.standing').textContent.trim()"
    )
    assert waiting.startswith("waiting")
    assert page.evaluate(
        f"document.querySelector('#apps-rows tr[data-id=\"{ids['swe']}\"] td.resume a').getAttribute('href')"
    ) == f"/api/resumes/{ids['sim']}/file"


def test_the_tabs_narrow_to_where_it_stands(page: Tab, served) -> None:
    tab_click(page, "rejected")
    page.wait_for(LOG, lambda titles: sorted(titles) == sorted([title("swe"), title("staff")]), timeout=5)
    assert page.evaluate(TABS)["all"] == "5", "the counts do not shrink with the tab"

    tab_click(page, "offer")
    page.wait_for(LOG, lambda titles: titles == [], timeout=5)
    assert "No offers yet" in page.evaluate("document.querySelector('#apps-rows').textContent")


def test_the_resume_table_says_what_each_one_got(page: Tab, served) -> None:
    # Sent, Waiting, OA, Interview, Offer, Rejected, Got a reply.
    assert page.evaluate(RESUMES) == {
        "sim": ["3", "1", "1", "1", "0", "1", "2 of 3"],
        "perception": ["1", "0", "0", "0", "0", "1", "0"],
        "No resume attached": ["1", "1", "0", "0", "0", "0", "0"],
    }


def test_a_resume_narrows_the_log_and_a_second_click_widens_it(page: Tab, served) -> None:
    _, _, ids = served
    resume_click(page, ids["sim"])
    page.wait_for(LOG, lambda titles: len(titles) == 3, timeout=5)
    assert page.evaluate(TABS) == {
        "all": "3", "waiting": "1", "oa": "0", "interview": "1", "offer": "0", "rejected": "1",
    }
    assert "Sent with sim" in page.evaluate("document.querySelector('#apps-narrowed').textContent")
    assert page.evaluate(NAV) == "5", "the sidebar still counts them all"

    tab_click(page, "waiting")
    page.wait_for(LOG, lambda titles: titles == [title("mech")], timeout=5)

    resume_click(page, ids["sim"])
    page.wait_for(LOG, lambda titles: len(titles) == 2, timeout=5)
    assert page.evaluate("document.querySelector('#apps-narrowed').hidden") is True

    resume_click(page, "none")
    page.wait_for(LOG, lambda titles: titles == [title("cleared")], timeout=5)
    page.evaluate("document.querySelector('#apps-resume-clear').click()")
    page.wait_for(LOG, lambda titles: len(titles) == 2, timeout=5)


def test_a_step_set_in_the_dialog_shows_once_it_closes(page: Tab, served) -> None:
    _, store, ids = served
    page.evaluate(f"document.querySelector('#apps-rows tr[data-id=\"{ids['mech']}\"] td.title').click()")
    page.wait_for("document.querySelector('#detail').open", bool, timeout=5)
    page.evaluate("document.querySelector('#d-body button.step[data-stage=\"offer\"]').click()")
    page.wait_for("1", lambda _: store.posting(ids["mech"])["outcome"] == "offer", timeout=5)

    page.evaluate("document.querySelector('#d-close').click()")
    page.wait_for(TABS, lambda tabs: tabs["offer"] == "1" and tabs["waiting"] == "1", timeout=5)
    page.wait_for(RESUMES, lambda found: found["sim"] == ["3", "0", "1", "1", "1", "1", "3 of 3"], timeout=5)


def test_rename_and_delete(page: Tab, served) -> None:
    _, store, ids = served
    # Both resumes are in use: neither offers Delete.
    assert page.evaluate("document.querySelectorAll('#resume-rows .resume-delete').length") == 0

    page.evaluate("window.prompt = () => '  Simulation v2 '")
    page.evaluate(f"document.querySelector('#resume-rows .resume-rename[data-id=\"{ids['sim']}\"]').click()")
    page.wait_for(RESUMES, lambda found: "Simulation v2" in found, timeout=5)
    assert len(page.evaluate(LOG)) == 5, "renaming is not a click on the row"

    store.set_application(ids["staff"], "applied", resume_id=None)
    tab_click(page, "waiting")  # any reload shows it is free now
    page.wait_for("document.querySelectorAll('#resume-rows .resume-delete').length", lambda n: n == 1, timeout=5)
    page.evaluate("document.querySelector('#resume-rows .resume-delete').click()")
    page.wait_for(RESUMES, lambda found: "perception" not in found, timeout=5)
    assert [r["name"] for r in store.resumes()] == ["Simulation v2"]
