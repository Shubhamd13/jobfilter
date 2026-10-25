"""Amazon Jobs fetcher.

A job page (amazon.jobs/en/jobs/{job_id}/{title}) has no JSON of its own: asking
for `.json` answers 406. The site's search does, unauthenticated:

    GET amazon.jobs/en/search.json?base_query={job_id}&result_limit=10

which returns the jobs matching the text. A job is found by searching for its
number and keeping the hit whose `id_icims` is that number; a search that does
not hold it means the job is gone.

A posting's text comes in three HTML parts: `description`,
`basic_qualifications` and `preferred_qualifications`. All three are joined
under the headings the page shows, so a reader can tell required from preferred.

Amazon as a whole is no board: it lists tens of thousands of jobs and the
search stops at 10,000 hits. A *team* is one. Every job names its team
("team-amazon-robotics"), the search filters on it

    GET amazon.jobs/en/search.json?team_category[]={team}&result_limit=100&offset=0

and each hit carries the full text, so a team of 400 jobs is four requests. So
a job's company slug is its team ("amazon-robotics"), which makes "every other
job at this company" and board refresh mean "this team". A team page
(amazon.jobs/en/teams/amazon-robotics) is accepted as a board URL. A job with
no team is filed under plain "amazon", which cannot be read as a board.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from urllib.parse import quote

from ..models import ParsedUrl, Platform, Posting
from .base import BoardNotFound, FetchError, JobNotFound, TransportFailure
from .http import HttpClient
from .textutil import html_to_text, join_sections

SITE = "https://www.amazon.jobs"
SEARCH = SITE + "/en/search.json?base_query={job_id}&result_limit=10"
TEAM_SEARCH = SITE + "/en/search.json?team_category%5B%5D={team}&result_limit={limit}&offset={offset}"
# The most the search returns per request; a larger limit answers with no jobs.
PAGE = 100
# A team larger than this is a job family ("team-sde-primary"), not a team
# anyone means to read whole.
MAX_BOARD_JOBS = 1500
# Where a job with no team is filed. Not a board: it would be all of Amazon.
SLUG = "amazon"
_NO_TEAM = frozenset({"", "no-sub-team-listed"})


def team_slug(team: str | None) -> str:
    """A team's company slug: "team-amazon-robotics" and "amazon-robotics" -> "amazon-robotics",
    "team-project-kuiper" -> "amazon-project-kuiper", no team -> "amazon"."""
    name = (team or "").strip().lower().removeprefix("team-")
    if name in _NO_TEAM:
        return SLUG
    return name if name.startswith("amazon") else f"amazon-{name}"


class AmazonFetcher:
    platform: ClassVar[Platform] = Platform.AMAZON

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self._jobs: dict[str, Posting] = {}

    def fetch(self, parsed: ParsedUrl) -> Posting:
        assert parsed.job_id  # guaranteed by url_parser
        job_id = parsed.job_id
        cached = self._jobs.get(job_id)
        if cached is not None:
            return cached

        api_url = SEARCH.format(job_id=quote(job_id))
        found = self._client.get_json(api_url, platform=self.platform)
        if found is not None and not isinstance(found, dict):
            raise TransportFailure(
                f"{api_url} returned unexpected JSON", url=parsed.url, platform=self.platform
            )
        jobs = (found or {}).get("jobs") or []
        job = next(
            (j for j in jobs if isinstance(j, dict) and str(j.get("id_icims")) == job_id), None
        )
        if job is None:
            raise JobNotFound(
                f"Amazon Jobs has no job {job_id!r} -- it may have been filled or taken down",
                url=parsed.url,
                platform=self.platform,
            )

        posting = _to_posting(job, job_id)
        self._jobs[job_id] = posting
        return posting

    def fetch_board(self, slug: str) -> list[Posting | FetchError]:
        """Every job of one team, a hundred per request."""
        if slug == SLUG:
            raise BoardNotFound(
                "Amazon as a whole is too large to read as one board; add a team page "
                "(amazon.jobs/en/teams/...) or single job URLs",
                url=SITE,
                platform=self.platform,
            )
        # "amazon-project-kuiper" was "team-project-kuiper"; "amazon-robotics", "team-amazon-robotics".
        for team in dict.fromkeys((f"team-{slug}", f"team-{slug.removeprefix('amazon-')}")):
            jobs = self._team_jobs(team, slug)
            if jobs:
                return jobs
        raise BoardNotFound(
            f"Amazon Jobs lists no jobs for team {slug!r}",
            url=f"{SITE}/en/teams/{slug}",
            platform=self.platform,
        )

    def fetch_board_url(self, parsed: ParsedUrl) -> list[Posting | FetchError]:
        """`amazon.jobs/en/teams/{team}`: the team is all a board needs."""
        assert parsed.slug  # guaranteed by url_parser
        return self.fetch_board(parsed.slug)

    def _team_jobs(self, team: str, slug: str) -> list[Posting | FetchError]:
        postings: dict[str, Posting] = {}
        offset = 0
        while True:
            url = TEAM_SEARCH.format(team=quote(team), limit=PAGE, offset=offset)
            found = self._client.get_json(url, platform=self.platform)
            if not isinstance(found, dict):
                raise TransportFailure(f"{url} returned unexpected JSON", url=url, platform=self.platform)
            hits = int(found.get("hits") or 0)
            if hits > MAX_BOARD_JOBS:
                raise BoardNotFound(
                    f"Amazon team {slug!r} lists {hits:,} jobs, too many to read as one board; "
                    f"add single job URLs instead",
                    url=f"{SITE}/en/teams/{slug}",
                    platform=self.platform,
                )
            page = [j for j in found.get("jobs") or [] if isinstance(j, dict) and j.get("id_icims")]
            for job in page:
                job_id = str(job["id_icims"])
                postings.setdefault(job_id, _to_posting(job, job_id))
            offset += PAGE
            if not page or offset >= hits:
                break
        self._jobs.update(postings)
        return list(postings.values())


def _to_posting(job: dict[str, Any], job_id: str) -> Posting:
    posted = " ".join(str(job.get("posted_date") or "").split())
    return Posting(
        platform=Platform.AMAZON,
        company_slug=team_slug((job.get("team") or {}).get("label")),
        job_id=job_id,
        title=job.get("title") or "",
        location=_location(job),
        url=SITE + (job.get("job_path") or f"/en/jobs/{job_id}"),
        description_text=join_sections(
            html_to_text(job.get("description") or ""),
            _section("Basic Qualifications", job.get("basic_qualifications")),
            _section("Preferred Qualifications", job.get("preferred_qualifications")),
        ),
        raw=job,
        posted_ago=f"Posted {posted}" if posted else None,
    )


def _section(heading: str, markup: str | None) -> str | None:
    text = html_to_text(markup or "")
    return f"{heading}\n{text}" if text else None


def _location(job: dict[str, Any]) -> str | None:
    """"Westboro, MA, USA", or several joined by "; ".

    Built from the city, region and country of each entry in `locations`
    rather than from `normalized_location`, which gets states wrong (this
    job's Westboro, MA came back as "Westboro, Wisconsin, USA").
    """
    places: list[str] = []
    for entry in job.get("locations") or []:
        try:
            place = json.loads(entry) if isinstance(entry, str) else entry
        except ValueError:
            continue
        if not isinstance(place, dict):
            continue
        parts = [place.get("city"), place.get("region"), place.get("countryIso3a")]
        text = ", ".join(str(p) for p in parts if p)
        # Amazon lists one office twice under two spellings: "N.Reading", "N.reading".
        if text and text.lower() not in {seen.lower() for seen in places}:
            places.append(text)
    if places:
        return "; ".join(places)

    # "US, MA, Westboro": country first, as the search result writes it.
    written = [p.strip() for p in str(job.get("location") or "").split(",") if p.strip()]
    return ", ".join(reversed(written)) or None
