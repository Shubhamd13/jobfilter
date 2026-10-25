"""The "apply by" date a posting states.

Mostly it is read from the description, on every platform alike:

    "Applications for this job will be accepted at least until October 4, 2026."  (NVIDIA)
    "Application deadline: Oct 12, 2026"
    "This posting will close on 10/12/2026 at 11:59 pm"
    "Posting Date: 09/30/2026 - 10/07/2026"                       (Caterpillar)

"At least until" is a minimum, not a close: the posting may stay up past it,
or come down any day after. `ApplyBy.firm` tells the two apart. A date with no
year takes the year that puts it nearest after the posting was first seen.

Failing that, Workday's own `endDate` field (`posting_apply_by`): the day the
board takes the posting down. Caterpillar sets it on nearly every posting, one
day after the date its text gives, so the day before is the last to apply.
No other board's JSON has a deadline field. Pure functions, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sept", "sep"), ("october", "oct"), ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))

# "October 4, 2026", "Oct. 4th 2026", "4 October 2026", "10/12/2026", "2026-10-12";
# the year is optional in the spelled-out forms.
_DATE = (
    rf"(?:(?P<mon>{_MONTH})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{{4}}))?"
    rf"|(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon2>{_MONTH})\.?(?:,?\s+(?P<year2>\d{{4}}))?"
    rf"|(?P<m>\d{{1,2}})/(?P<d>\d{{1,2}})/(?P<y>\d{{4}}|\d{{2}})"
    rf"|(?P<iy>\d{{4}})-(?P<im>\d{{2}})-(?P<id>\d{{2}}))"
)

# What comes right before the date. `least` marks a minimum, not a close.
_LEAD = (
    r"(?:accepted|considered|open|posted|accepting applications)\s+(?P<least>at\s+least\s+)?"
    r"(?:until|through|thru|till)"
    r"|(?:application|applications|apply|submission)\s+(?:deadline|due date|close date|closing date)\s*(?:is|:|-)?"
    r"|deadline\s+(?:to\s+apply|for\s+applications|for\s+applying)\s*(?:is|:|-)?"
    r"|closing\s+date\s*(?:is|:|-)?"
    r"|apply\s+(?:by|before|no\s+later\s+than)"
    r"|(?:applications?|posting|position|role|requisition|job)\s+(?:will\s+)?(?:close|closes|expire|expires)\s+(?:on\s+)?"
    r"|(?:applications?|submissions?)\s+(?:are\s+)?due\s+(?:by\s+|on\s+)?"
    # A posting window: the date after the dash is when it ends.
    rf"|posting\s+dates?\s*:?\s*{_DATE.replace('?P<', '?P<from_')}\s*(?:-|–|to)"
)
# "close at 11:59 p.m. on 10/15/2026"
_TIME = r"(?:at\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?\s*(?:[A-Z]{2,4}\s+)?)?"

_APPLY_BY = re.compile(
    rf"\b(?:{_LEAD})\s*{_TIME}(?:on\s+)?(?:\w+day,?\s+)?{_DATE}\b", re.IGNORECASE
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s|\n")


@dataclass(frozen=True, slots=True)
class ApplyBy:
    on: date
    # The sentence that gave it, for the UI to quote.
    sentence: str
    # False for "at least until": the posting may outlast the date.
    firm: bool


def apply_by(text: str | None, seen: date) -> ApplyBy | None:
    """The first apply-by date the text states, or None.

    `seen` is when the posting was first seen: it places a date with no year.
    """
    for match in _APPLY_BY.finditer(text or ""):
        on = _date(match, seen)
        if on is not None:
            return ApplyBy(on, _sentence(text or "", match), firm=not match["least"])
    return None


def posting_apply_by(text: str | None, raw: Mapping[str, Any], seen: date) -> ApplyBy | None:
    """The description's apply-by date, else the board's own end date."""
    return apply_by(text, seen) or _board_end_date(raw)


def _board_end_date(raw: Mapping[str, Any]) -> ApplyBy | None:
    """Workday's `endDate` ("2026-10-11"), as the day before it."""
    end = raw.get("endDate")
    if not isinstance(end, str):
        return None
    try:
        last = date.fromisoformat(end[:10])
    except ValueError:
        return None
    sentence = f"Workday takes this posting down on {last:%B} {last.day}, {last.year}."
    return ApplyBy(last - timedelta(days=1), sentence, firm=True)


def _date(match: re.Match[str], seen: date) -> date | None:
    """The match's date, or None when it names no real day."""
    if match["iy"]:
        year, month, day = int(match["iy"]), int(match["im"]), int(match["id"])
    elif match["y"]:
        # US order: 10/12/2026 is October 12.
        year, month, day = int(match["y"]), int(match["m"]), int(match["d"])
        year += 2000 if year < 100 else 0
    else:
        month = _MONTHS[(match["mon"] or match["mon2"]).lower()]
        day = int(match["day"] or match["day2"])
        stated = match["year"] or match["year2"]
        if stated is None:
            return _nearest_after(seen, month, day)
        year = int(stated)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _nearest_after(seen: date, month: int, day: int) -> date | None:
    """The yearless date as the first one on or after `seen`, give or take a month.

    A posting first seen on Oct 2 that says "until October 4" means this year;
    one that says "until January 15" means next. A date a few weeks before
    `seen` stays in its year: the posting is just past its deadline.
    """
    for year in (seen.year - 1, seen.year, seen.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if (candidate - seen).days >= -31:
            return candidate
    return None


def _sentence(text: str, match: re.Match[str]) -> str:
    """The sentence around a match, whitespace collapsed."""
    starts = [m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())]
    start = starts[-1] if starts else 0
    end_match = _SENTENCE_END.search(text, match.end())
    end = end_match.start() if end_match else len(text)
    return " ".join(text[start:end].split())
