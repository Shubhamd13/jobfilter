"""The day a posting went up, from what its board gave us.

Boards with an API put an exact date in the posting's JSON, each under its own
key. LinkedIn and Indeed pages give only "2 weeks ago", which counts back from
when the page was read (`listing_seen_at`); "weeks", "months" and "30+ days"
are approximate.

"Posted" is the board's own date. Some keep it across a repost (Greenhouse's
`first_published`), so it means "first posted here", not "last refreshed".
Pure functions, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

# Where each platform's raw JSON keeps the posting date, first key first.
DATE_KEYS: dict[str, tuple[str, ...]] = {
    "workday": ("startDate",),
    "greenhouse": ("first_published", "updated_at"),
    "eightfold": ("postedTs", "creationTs"),
    "smartrecruiters": ("releasedDate",),
    "lever": ("createdAt",),
    "ashby": ("publishedAt",),
    "amazon": ("posted_date",),
    "rippling": ("createdOn",),
    "workable": ("published_on", "created_at"),
    "dover": ("created",),
}

# "3 days ago", "Reposted 5 hours ago", "Posted 30+ Days Ago", "1 month ago".
_AGO = re.compile(
    r"(?P<n>\d+)(?P<plus>\+)?\s*(?P<unit>minute|min|hour|hr|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_UNIT_DAYS = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30, "year": 365}


@dataclass(frozen=True, slots=True)
class PostedOn:
    on: date
    # True when counted back from "2 weeks ago" and the like.
    approximate: bool = False


def posted_on(
    platform: str,
    raw: Mapping[str, Any],
    posted_ago: str | None = None,
    listing_seen_at: str | None = None,
) -> PostedOn | None:
    """When the posting went up: its board's date, else its "N days ago" text."""
    for key in DATE_KEYS.get(platform, ()):
        day = parse_day(raw.get(key))
        if day is not None:
            return PostedOn(day)
    return _from_ago(posted_ago, listing_seen_at)


def parse_day(value: Any) -> date | None:
    """A local date from a timestamp (seconds or milliseconds), ISO text, or "October 2, 2026"."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            # Midnight UTC exactly is a bare date (Eightfold's postedTs), not a
            # moment: read where you are, it would land the day before.
            if seconds % 86400 == 0:
                return datetime.fromtimestamp(seconds, timezone.utc).date()
            return datetime.fromtimestamp(seconds).date()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        # A moment with a zone is a day where you are; one without, as written.
        return moment.astimezone().date() if moment.tzinfo else moment.date()
    try:
        return datetime.strptime(text, "%B %d, %Y").date()
    except ValueError:
        return None


def _from_ago(posted_ago: str | None, listing_seen_at: str | None) -> PostedOn | None:
    """Count "3 days ago" / "Posted Yesterday" back from when it was read."""
    if not posted_ago or not listing_seen_at:
        return None
    seen = parse_day(listing_seen_at)
    if seen is None:
        return None
    text = posted_ago.lower()
    if "today" in text or "just" in text:
        return PostedOn(seen)
    if "yesterday" in text:
        return PostedOn(seen - timedelta(days=1))
    match = _AGO.search(text)
    if match is None:
        return None
    unit = match["unit"].lower()
    days = int(match["n"]) * _UNIT_DAYS[unit]
    approximate = bool(match["plus"]) or unit in ("week", "month", "year")
    return PostedOn(seen - timedelta(days=days), approximate)
