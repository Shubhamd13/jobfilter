"""Posting dates from each board's JSON, or from "N days ago" text."""

from __future__ import annotations

from datetime import date

import pytest

from jobfilter.posted import PostedOn, parse_day, posted_on

SEEN_AT = "2026-10-16T15:00:00+00:00"


@pytest.mark.parametrize(
    "platform,raw,expected",
    [
        pytest.param("workday", {"startDate": "2026-09-30", "postedOn": "Posted Yesterday"}, date(2026, 9, 30), id="workday"),
        pytest.param("greenhouse", {"first_published": "2026-09-08T14:21:25-04:00", "updated_at": "2026-09-09T13:52:39-04:00"}, date(2026, 9, 8), id="greenhouse-first-published"),
        pytest.param("eightfold", {"postedTs": 1789603200}, date(2026, 9, 17), id="eightfold-midnight-utc-is-a-date"),
        pytest.param("smartrecruiters", {"releasedDate": "2026-10-02T15:41:49.451Z"}, date(2026, 10, 2), id="smartrecruiters"),
        pytest.param("ashby", {"publishedAt": "2026-08-26T16:39:41.564+00:00"}, date(2026, 8, 26), id="ashby"),
        pytest.param("amazon", {"posted_date": "October  2, 2026"}, date(2026, 10, 2), id="amazon-double-space"),
        pytest.param("workable", {"published_on": "2026-09-16", "created_at": "2026-09-15"}, date(2026, 9, 16), id="workable"),
        pytest.param("rippling", {"createdOn": "2026-05-21T07:55:46.164000-07:00"}, date(2026, 5, 21), id="rippling"),
        pytest.param("dover", {"created": "2026-08-11T16:07:00.546301Z"}, date(2026, 8, 11), id="dover"),
    ],
)
def test_board_dates(platform: str, raw: dict, expected: date) -> None:
    assert posted_on(platform, raw) == PostedOn(expected)


def test_lever_milliseconds() -> None:
    found = posted_on("lever", {"createdAt": 1788288555846})
    assert found is not None and found.on in (date(2026, 9, 1), date(2026, 9, 2))


@pytest.mark.parametrize(
    "text,expected,approximate",
    [
        pytest.param("3 hours ago", date(2026, 10, 16), False, id="hours"),
        pytest.param("Reposted 1 day ago", date(2026, 10, 15), False, id="reposted"),
        pytest.param("5 days ago", date(2026, 10, 11), False, id="days"),
        pytest.param("2 weeks ago", date(2026, 10, 2), True, id="weeks"),
        pytest.param("1 month ago", date(2026, 9, 16), True, id="month"),
        pytest.param("Posted 30+ Days Ago", date(2026, 9, 16), True, id="plus-days"),
        pytest.param("Posted Today", date(2026, 10, 16), False, id="today"),
        pytest.param("Posted Yesterday", date(2026, 10, 15), False, id="yesterday"),
    ],
)
def test_relative_text_counts_back_from_when_it_was_read(text: str, expected: date, approximate: bool) -> None:
    assert posted_on("linkedin", {}, text, SEEN_AT) == PostedOn(expected, approximate)


@pytest.mark.parametrize(
    "platform,raw,posted_ago,seen_at",
    [
        pytest.param("indeed", {}, None, None, id="nothing"),
        pytest.param("linkedin", {}, "2 weeks ago", None, id="text-without-when-read"),
        pytest.param("linkedin", {}, "Promoted", SEEN_AT, id="no-time-in-text"),
        pytest.param("greenhouse", {"first_published": "soon"}, None, None, id="garbage"),
        pytest.param("web", {"startDate": "2026-09-30"}, None, None, id="key-of-another-platform"),
    ],
)
def test_no_date(platform: str, raw: dict, posted_ago: str | None, seen_at: str | None) -> None:
    assert posted_on(platform, raw, posted_ago, seen_at) is None


@pytest.mark.parametrize("value", [None, True, "", "  ", "next week", {}], ids=repr)
def test_parse_day_rejects(value: object) -> None:
    assert parse_day(value) is None
