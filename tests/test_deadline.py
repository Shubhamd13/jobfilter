"""Apply-by dates read from posting text. Most cases are sentences from real postings."""

from __future__ import annotations

from datetime import date

import pytest

from jobfilter.deadline import apply_by

SEEN = date(2026, 10, 2)


@pytest.mark.parametrize(
    "text,on,firm",
    [
        pytest.param(
            "Applications for this job will be accepted at least until October 4, 2026.",
            date(2026, 10, 4), False, id="nvidia-minimum",
        ),
        pytest.param("Applications will be accepted until November 8, 2026.", date(2026, 11, 8), True, id="accepted-until"),
        pytest.param("Application deadline: Oct 12, 2026", date(2026, 10, 12), True, id="deadline-colon"),
        pytest.param("- This posting will close on 10/12/2026 at 11:59 p.m.", date(2026, 10, 12), True, id="us-numeric"),
        pytest.param("- This position will close on 8/26/26", date(2026, 8, 26), True, id="two-digit-year"),
        pytest.param("This position will close at 11:59 p.m. on 10/15/2026.", date(2026, 10, 15), True, id="time-before-date"),
        pytest.param("Posting Date: 09/30/2026 - 10/07/2026  Summary Pay Range", date(2026, 10, 7), True, id="posting-window-end"),
        pytest.param("Please apply by Friday, Nov 6th.", date(2026, 11, 6), True, id="weekday-and-ordinal"),
        pytest.param("Deadline to apply: 2026-10-20", date(2026, 10, 20), True, id="iso"),
        pytest.param("Application deadline: 15 January", date(2027, 1, 15), True, id="no-year-rolls-forward"),
        pytest.param("Accepting applications through Sept. 30", date(2026, 9, 30), True, id="no-year-just-past"),
    ],
)
def test_apply_by(text: str, on: date, firm: bool) -> None:
    found = apply_by(text, SEEN)
    assert found is not None
    assert (found.on, found.firm) == (on, firm)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Prioritize tasks to meet tight deadlines.", id="deadlines-as-work"),
        pytest.param("Valid passport through August 2027.", id="passport"),
        pytest.param("Modern C++ (C++14/17/20).", id="cpp-versions"),
        pytest.param("This is a paid internship running October 2026 through March 2027.", id="internship-dates"),
        pytest.param("Application deadline: February 30, 2026", id="not-a-real-day"),
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
    ],
)
def test_no_apply_by(text: str | None) -> None:
    assert apply_by(text, SEEN) is None


def test_the_sentence_is_quoted_whole_across_a_line_break() -> None:
    text = (
        "You will also be eligible for equity and benefits. Applications for this job "
        "will be accepted at least until\nOctober 4, 2026. This posting is for an existing vacancy."
    )
    found = apply_by(text, SEEN)
    assert found is not None
    assert found.sentence == (
        "Applications for this job will be accepted at least until October 4, 2026."
    )


def test_workday_end_date_is_the_fallback_a_day_early() -> None:
    """Caterpillar's endDate is one day after the date its text gives."""
    from jobfilter.deadline import posting_apply_by

    found = posting_apply_by("No date in the text.", {"endDate": "2026-10-11"}, SEEN)
    assert found is not None
    assert (found.on, found.firm) == (date(2026, 10, 10), True)
    assert found.sentence == "Workday takes this posting down on October 11, 2026."


def test_the_description_beats_the_end_date() -> None:
    from jobfilter.deadline import posting_apply_by

    text = "Applications for this job will be accepted at least until October 4, 2026."
    found = posting_apply_by(text, {"endDate": "2026-12-01"}, SEEN)
    assert found is not None
    assert (found.on, found.firm) == (date(2026, 10, 4), False)


@pytest.mark.parametrize("raw", [{}, {"endDate": None}, {"endDate": "soon"}], ids=["none", "null", "garbage"])
def test_no_usable_end_date(raw: dict) -> None:
    from jobfilter.deadline import posting_apply_by

    assert posting_apply_by("No date.", raw, SEEN) is None
