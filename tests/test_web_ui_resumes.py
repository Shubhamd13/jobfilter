"""The page's resume picker and upload, in a real (headless) Chrome.

Same harness as test_web_ui.py. Skipped when Chrome is missing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from jobfilter.storage import Store

from .cdp import Chrome, Tab
from .jsrunner import find_chrome
from .test_resumes import OTHER_PDF, PDF
from .test_web_ui import chrome, load  # noqa: F401 - chrome is a fixture
from .test_web_ui_marks import press, served  # noqa: F401 - served is a fixture
from .test_web_ui_outcomes import open_dialog

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="needs Chrome/Chromium")

OPTIONS = "[...document.querySelectorAll('#d-body .resume-pick option')].map(o => o.textContent)"
PICKED = "document.querySelector('#d-body .resume-pick')?.selectedOptions[0]?.textContent"
ERROR = "document.querySelector('#d-body .mine-error')?.hidden === false ? document.querySelector('#d-body .mine-error').textContent : ''"


@pytest.fixture
def page(chrome: Chrome, served: tuple[str, Store, dict[str, int]]) -> Iterator[Tab]:  # noqa: F811
    tab = chrome.open_tab()
    load(tab, served[0])
    yield tab
    tab.close()


def choose_file(tab: Tab, name: str, data: bytes) -> None:
    """Put a file in the dialog's file input, as the file chooser would."""
    tab.evaluate(
        "(() => {"
        f"  const file = new File([new Uint8Array({list(data)})], {name!r});"
        "  const files = new DataTransfer(); files.items.add(file);"
        "  const input = document.querySelector('#d-body .resume-file');"
        "  input.files = files.files;"
        "  input.dispatchEvent(new Event('change', { bubbles: true }));"
        "})()"
    )


def pick(tab: Tab, value: object) -> None:
    tab.evaluate(
        "(() => {"
        "  const select = document.querySelector('#d-body .resume-pick');"
        f"  select.value = '{value}';"
        "  select.dispatchEvent(new Event('change', { bubbles: true }));"
        "})()"
    )


def test_the_picker_shows_once_a_posting_is_applied(page: Tab, served) -> None:
    _, _, ids = served
    open_dialog(page, ids["swe"])
    assert page.evaluate("document.querySelector('#d-body .resume-row')") is None

    press(page, ids["swe"], "applied", within="#d-body")
    page.wait_for(OPTIONS, lambda found: found == ["none attached"], timeout=5)
    assert page.evaluate("document.querySelector('#d-body .resume-open')") is None


def test_uploading_a_file_stores_and_attaches_it(page: Tab, served) -> None:
    _, store, ids = served
    store.set_application(ids["swe"], "applied")
    open_dialog(page, ids["swe"])

    choose_file(page, "Alex_Sim.pdf", PDF)
    page.wait_for(PICKED, lambda name: name == "Alex_Sim", timeout=5)

    resume = store.resumes()[0]
    assert store.posting(ids["swe"])["resume_id"] == resume["id"]
    assert store.resume_file(resume["id"])["data"] == PDF
    assert page.evaluate("document.querySelector('#d-body .resume-open').getAttribute('href')") == (
        f"/api/resumes/{resume['id']}/file"
    )
    assert store.posting(ids["swe"])["app_status"] == "applied"


def test_a_resume_uploaded_before_can_be_picked_and_detached(page: Tab, served) -> None:
    url, store, ids = served
    first, _ = store.add_resume("simulation", "sim.pdf", "application/pdf", PDF)
    second, _ = store.add_resume("perception", "perception.pdf", "application/pdf", OTHER_PDF)
    store.set_application(ids["swe"], "applied", "sent through a referral")
    load(page, url)
    open_dialog(page, ids["swe"])
    assert sorted(page.evaluate(OPTIONS)) == ["none attached", "perception", "simulation"]

    pick(page, second["id"])
    page.wait_for("1", lambda _: store.posting(ids["swe"])["resume_id"] == second["id"], timeout=5)
    page.wait_for(PICKED, lambda name: name == "perception", timeout=5)
    assert store.posting(ids["swe"])["app_note"] == "sent through a referral", "the note is left alone"

    pick(page, "")
    page.wait_for("1", lambda _: store.posting(ids["swe"])["resume_id"] is None, timeout=5)
    page.wait_for(PICKED, lambda name: name == "none attached", timeout=5)
    assert len(store.resumes()) == 2, "detaching keeps the file"


def test_the_same_file_on_a_second_posting_is_the_same_resume(page: Tab, served) -> None:
    _, store, ids = served
    store.set_application(ids["swe"], "applied")
    store.set_application(ids["senior"], "applied")

    for job in ("swe", "senior"):
        open_dialog(page, ids[job])
        choose_file(page, f"copy for {job}.pdf", PDF)
        page.wait_for("1", lambda _, job=job: store.posting(ids[job])["resume_id"] is not None, timeout=5)
        page.evaluate("document.querySelector('#d-close').click()")

    assert len(store.resumes()) == 1
    assert store.resumes()[0]["used"] == 2


def test_a_file_that_is_not_a_resume_is_refused_in_the_dialog(page: Tab, served) -> None:
    _, store, ids = served
    store.set_application(ids["swe"], "applied")
    open_dialog(page, ids["swe"])

    choose_file(page, "notes.txt", b"hello")
    page.wait_for(ERROR, lambda text: ".pdf or .docx" in text, timeout=5)
    assert store.resumes() == [] and store.posting(ids["swe"])["resume_id"] is None
