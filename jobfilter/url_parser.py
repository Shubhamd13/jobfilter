"""URL -> ParsedUrl. Pure functions, no network, never raises.

Supported shapes (scheme optional, `www.` optional, query/fragment ignored):

    ashby       jobs.ashbyhq.com/{slug}/{job_id}
    greenhouse  boards.greenhouse.io/{slug}/jobs/{job_id}
                job-boards.greenhouse.io/{slug}/jobs/{job_id}
                {boards,job-boards}.greenhouse.io/embed/job_app?for={slug}&token={job_id}
                {boards,job-boards}.greenhouse.io/{slug}?gh_jid={job_id}

Greenhouse URLs that name the job but not the board come back with
`slug=None`; GreenhouseFetcher looks the slug up from the job id:

                any-company-site.com/careers?gh_jid={job_id}
                boards.greenhouse.io/embed/job_app?token={job_id}

An Ashby embed on a company site likewise comes back with `slug=None`;
AshbyFetcher finds the board from the page or the domain:

                any-company-site.com/careers?ashby_jid={job_id}
    lever       jobs.lever.co/{slug}/{job_id}
    dover       app.dover.com/apply/{Company Name}/{job_id}
    rippling    ats.rippling.com/[{locale}/]{slug}/jobs/{job_id}
                ats.rippling.com/[{locale}/]{slug}[/jobs]  a whole board: job_id=None
    smartrecruiters
                jobs.smartrecruiters.com/{Company}/{job_id}[-{title}]
                {jobs,careers}.smartrecruiters.com/{Company}          a whole board: job_id=None
                api.smartrecruiters.com/v1/companies/{Company}/postings[/{job_id}]
    workable    apply.workable.com/{account}/j/{shortcode}
                apply.workable.com/j/{shortcode}                      slug=None: the fetcher follows the redirect
                apply.workable.com/{account}                          a whole board: job_id=None
                apply.workable.com/api/v1/widget/accounts/{account}   the same, as the API writes it
                apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}
                jobs.workable.com/view/{view_id}[/{title}]            slug=None: the fetcher finds the account
                jobs.workable.com/company/{id}/jobs-at-{account}      a whole board
    eightfold   {any host}/careers/job/{position_id}
                {any host}/careers?pid={position_id}                  the older share link
                {any host}/api/apply/v2/jobs/{position_id}[?domain=]  the API, as a job URL
                {any host}/api/pcsx/position_details?position_id={position_id}
                {any host}/api/apply/v2/jobs?query=..., /api/pcsx/search?...   a search: job_id=None
                {known host}/careers[?query=...&location=...]         the same, as the page writes it
    amazon     amazon.jobs/[{locale}/]jobs/{job_id}[/{title}]
                account.amazon.jobs/jobs/{job_id}/apply   (the apply link)
                amazon.jobs/[{locale}/]teams/{team}       a team's board: job_id=None
                amazon.jobs/[{locale}/]search?team_category[]={team}   the same
    workday     {tenant}.wd{N}.myworkdayjobs.com/[{locale}/]{site}/job/[{location}/]{Title}_{job_id}
                ... /{site}/details/{Title}_{job_id}  (the pane beside search results)
                ... /{site}[?facets]                  a whole board: job_id=None

An Amazon job URL does not name its team, so it parses with the slug "amazon";
the posting AmazonFetcher returns carries the team's slug ("amazon-robotics").
A team URL parses to that slug directly.
A Dover URL's slug is its company name slugified ("Alloy%20Robotics" ->
"alloy-robotics"), the slug the browser extension gives LinkedIn's
"Alloy Robotics". A Workday URL's slug is the tenant. The fetcher also needs the host and the
site name, which `workday_address` reads from the same URL.
An Eightfold site lives on the employer's own domain, so its slug is the
host's company name ("careers.qualcomm.com" -> "qualcomm"); `eightfold_address`
reads the host and any search from the same URL.

Anything else -- including a known host with a path we don't recognize, such as
a company board URL with no job id -- comes back as Platform.UNSUPPORTED rather
than raising, so the CLI can report it instead of dropping it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from .models import ParsedUrl, Platform

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

# Greenhouse job ids are numeric; anything else in gh_jid/token is not one.
_GH_JOB_ID_RE = re.compile(r"[0-9]+")
# Ashby and Dover job ids are UUIDs.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

# SmartRecruiters: "744000153260889-robotic-algorithm-and-controls-engineer".
_SR_JOB_RE = re.compile(r"([0-9]{6,})(?:-.*)?")
_SR_COMPANY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

# Workable: a careers-site shortcode ("3FF60B88A4"), and the longer mixed-case id
# of the same job on Workable's job board ("nPbcR1G5FfkYVf2cezffrL").
_WORKABLE_SHORTCODE_RE = re.compile(r"[0-9A-Fa-f]{10}")
_WORKABLE_VIEW_RE = re.compile(r"[A-Za-z0-9]{16,32}")
_WORKABLE_ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")

# Amazon job ids are numbers ("10568411"); older ones are shorter.
_AMAZON_JOB_ID_RE = re.compile(r"[0-9]{4,12}")

# Trailing segments that belong to the apply flow, not to the job id.
_APPLY_SEGMENTS = frozenset({"application", "apply", "application_form"})

_HOSTS: dict[str, Platform] = {
    "jobs.ashbyhq.com": Platform.ASHBY,
    "boards.greenhouse.io": Platform.GREENHOUSE,
    "job-boards.greenhouse.io": Platform.GREENHOUSE,
    "jobs.lever.co": Platform.LEVER,
    "app.dover.com": Platform.DOVER,
    "ats.rippling.com": Platform.RIPPLING,
    "jobs.smartrecruiters.com": Platform.SMARTRECRUITERS,
    "careers.smartrecruiters.com": Platform.SMARTRECRUITERS,
    "api.smartrecruiters.com": Platform.SMARTRECRUITERS,
    "apply.workable.com": Platform.WORKABLE,
    "jobs.workable.com": Platform.WORKABLE,
    "amazon.jobs": Platform.AMAZON,
    "account.amazon.jobs": Platform.AMAZON,
}


def parse_url(url: str) -> ParsedUrl:
    """Parse a job posting URL into its platform, company slug and job id."""
    split = _split(url)
    if split is None:
        return _unsupported(url)

    host, segments, query = split
    platform = _HOSTS.get(host)
    if platform is None:
        if host.endswith(_WORKDAY_HOST_SUFFIX):
            return _parse_workday(url)
        return _parse_company_site(url, query)

    parts = _PATH_PARSERS[platform](segments, query)
    if parts is None:
        return _unsupported(url)

    slug, job_id = parts
    return ParsedUrl(platform=platform, slug=slug, job_id=job_id, url=url.strip())


def _split(url: str) -> tuple[str, list[str], dict[str, str]] | None:
    """Return (host, non-empty path segments, first value of each query param),
    or None if this isn't an http URL."""
    raw = url.strip()
    if not raw or any(ch.isspace() for ch in raw):
        return None

    scheme = _SCHEME_RE.match(raw)
    if scheme is not None:
        if scheme.group(0).lower() not in ("http://", "https://"):
            return None
    elif ":" in raw.split("/", 1)[0]:
        return None  # a non-http scheme such as "mailto:" or "ftp:"
    else:
        raw = "https://" + raw  # bare "jobs.lever.co/acme/123"

    try:
        parts = urlsplit(raw)
    except ValueError:  # e.g. an invalid IPv6 literal
        return None

    host = (parts.hostname or "").removeprefix("www.")
    if not host:
        return None

    segments = [unquote(s) for s in parts.path.split("/") if s]
    query = {k: v[0].strip() for k, v in parse_qs(parts.query).items()}
    return host, _strip_apply_segments(segments), query


def _strip_apply_segments(segments: list[str]) -> list[str]:
    end = len(segments)
    while end > 0 and segments[end - 1].lower() in _APPLY_SEGMENTS:
        end -= 1
    return segments[:end]


def _parse_ashby(segments: list[str], query: dict[str, str]) -> tuple[str, str] | None:
    # jobs.ashbyhq.com/{slug}/{job_id}
    if len(segments) == 2:
        return segments[0], segments[1]
    return None


def _parse_greenhouse(
    segments: list[str], query: dict[str, str]
) -> tuple[str | None, str] | None:
    # {boards,job-boards}.greenhouse.io/{slug}/jobs/{job_id}
    if len(segments) == 3 and segments[1].lower() == "jobs":
        return segments[0], segments[2]
    # The iframe a company careers page embeds: .../embed/job_app?for={slug}&token={job_id}
    # (any validityToken param is only for the apply form and is ignored).
    # Without `for` the slug is unknown and the fetcher resolves it.
    if [s.lower() for s in segments] == ["embed", "job_app"]:
        job_id = query.get("token", "")
        if _GH_JOB_ID_RE.fullmatch(job_id):
            return query.get("for") or None, job_id
        return None
    # {boards,job-boards}.greenhouse.io/{slug}?gh_jid={job_id}
    job_id = query.get("gh_jid", "")
    if len(segments) == 1 and _GH_JOB_ID_RE.fullmatch(job_id):
        return segments[0], job_id
    return None


def _parse_company_site(url: str, query: dict[str, str]) -> ParsedUrl:
    # A company careers page hosting a Greenhouse or Ashby embed:
    # {anything}?gh_jid={job_id} or {anything}?ashby_jid={job_id}. The board
    # slug is not in the URL; the platform's fetcher resolves it.
    job_id = query.get("gh_jid", "")
    if _GH_JOB_ID_RE.fullmatch(job_id):
        return ParsedUrl(
            platform=Platform.GREENHOUSE, slug=None, job_id=job_id, url=url.strip()
        )
    job_id = query.get("ashby_jid", "")
    if _UUID_RE.fullmatch(job_id):
        return ParsedUrl(
            platform=Platform.ASHBY, slug=None, job_id=job_id.lower(), url=url.strip()
        )
    address = eightfold_address(url)
    if address is not None:
        return ParsedUrl(
            platform=Platform.EIGHTFOLD, slug=address.slug, job_id=address.job_id, url=url.strip()
        )
    return _unsupported(url)


def _parse_lever(segments: list[str], query: dict[str, str]) -> tuple[str, str] | None:
    # jobs.lever.co/{slug}/{job_id}
    if len(segments) == 2:
        return segments[0], segments[1]
    return None


# --- Workday ---------------------------------------------------------------

_WORKDAY_HOST_SUFFIX = ".myworkdayjobs.com"
_LOCALE_RE = re.compile(r"[a-z]{2}(?:-[a-z]{2,4})?", re.IGNORECASE)
_WORKDAY_TITLE_RE = re.compile(r".*_([A-Za-z0-9][A-Za-z0-9-]*)")
_WORKDAY_JOB_MARKERS = frozenset({"job", "details"})


@dataclass(frozen=True, slots=True)
class WorkdayAddress:
    """Everything a Workday URL says about where its board and job live.

    `job_path` is the path below `/job/` as it appears in the URL (still
    percent-encoded), e.g. `US-CA-Santa-Clara/Engineer_JR123`; None for a board.
    `query` keeps every value of every parameter: Workday repeats a facet
    parameter once per selected value.
    """

    host: str
    tenant: str
    site: str
    job_path: str | None
    job_id: str | None
    query: dict[str, list[str]]


def workday_address(url: str) -> WorkdayAddress | None:
    """Read a myworkdayjobs.com URL; None if it is not a board or job page."""
    raw = url.strip()
    if not _SCHEME_RE.match(raw):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    # {tenant}.wd5.myworkdayjobs.com: the tenant is the first label.
    if not host.endswith(_WORKDAY_HOST_SUFFIX):
        return None
    tenant = host.removesuffix(_WORKDAY_HOST_SUFFIX).split(".", 1)[0]
    if not tenant:
        return None
    # Raw segments: the job path goes back into an API URL as it came.
    segments = [s for s in parts.path.split("/") if s]
    if segments and _LOCALE_RE.fullmatch(segments[0]):
        segments = segments[1:]
    if not segments:
        return None
    site, rest = segments[0], segments[1:]
    query = parse_qs(parts.query)

    if not rest:
        return WorkdayAddress(host, tenant, site, None, None, query)
    if rest[0].lower() not in _WORKDAY_JOB_MARKERS:
        return None  # sign-in, user home, ...
    # The first segment ending in _{id} is the title; a location segment may
    # come before it, and /apply/... after it.
    for index, segment in enumerate(rest[1:], start=1):
        match = _WORKDAY_TITLE_RE.fullmatch(unquote(segment))
        if match:
            job_path = "/".join(rest[1 : index + 1])
            return WorkdayAddress(host, tenant, site, job_path, match.group(1), query)
    return None


def _parse_workday(url: str) -> ParsedUrl:
    address = workday_address(url)
    if address is None:
        return _unsupported(url)
    return ParsedUrl(
        platform=Platform.WORKDAY,
        slug=address.tenant,
        job_id=address.job_id,
        url=url.strip(),
    )


_SLUG_JUNK_RE = re.compile(r"[^a-z0-9]+")


def slugify_company(name: str) -> str:
    """"Alloy Robotics" -> "alloy-robotics"."""
    return _SLUG_JUNK_RE.sub("-", name.lower()).strip("-")


# --- Eightfold ---------------------------------------------------------------

# Eightfold's own hosts serve many employers; `domain=` says which.
_EIGHTFOLD_HOST_SUFFIX = ".eightfold.ai"
# Employer hosts known to run Eightfold, where a bare /careers search page is a
# board. A job path (/careers/job/{id}, /api/...) is Eightfold's on any host.
_EIGHTFOLD_HOSTS = frozenset({
    "careers.qualcomm.com",
    "apply.careers.microsoft.com",
    "explore.jobs.netflix.net",
})
# Position ids are long numbers: 446720880729 (Qualcomm), 1970393557037678 (Microsoft).
_EIGHTFOLD_ID_RE = re.compile(r"[0-9]{9,20}")
_EIGHTFOLD_JOB_SEGMENT_RE = re.compile(r"([0-9]{9,20})(?:-[^/]*)?")
# Labels that are not a company name: careers.example.co.uk is "example".
_SECOND_LEVEL_LABELS = frozenset({"co", "com", "ac", "org", "net", "gov", "edu"})
_EIGHTFOLD_SEARCH_PARAMS = frozenset({"query", "location"})


@dataclass(frozen=True, slots=True)
class EightfoldAddress:
    """Everything an Eightfold URL says about its site and job or search.

    `domain` is the URL's `domain=` parameter, if any: the fetcher reads the
    real one off the site's page. `search` keeps a search URL's query,
    location and `filter_*` parameters; it is empty for a job.
    """

    host: str
    domain: str | None
    slug: str
    job_id: str | None
    search: dict[str, str]


def eightfold_address(url: str) -> EightfoldAddress | None:
    """Read an Eightfold job or search URL; None if it is neither."""
    split = _split(url)
    if split is None:
        return None
    host, segments, query = split
    host = host.lower()
    domain = query.get("domain", "").lower() or None
    known = host in _EIGHTFOLD_HOSTS or host.endswith(_EIGHTFOLD_HOST_SUFFIX)
    lower = [s.lower() for s in segments]

    job_id: str | None = None
    if len(lower) == 3 and lower[:2] == ["careers", "job"]:
        match = _EIGHTFOLD_JOB_SEGMENT_RE.fullmatch(segments[2])
        if match is None:
            return None
        job_id = match.group(1)
    elif lower == ["careers"] and "pid" in query:
        job_id = query["pid"]
    elif len(lower) == 5 and lower[:4] == ["api", "apply", "v2", "jobs"]:
        job_id = segments[4]
    elif lower == ["api", "pcsx", "position_details"]:
        job_id = query.get("position_id", "")
    elif lower in (["api", "apply", "v2", "jobs"], ["api", "pcsx", "search"]):
        pass  # the search API: Eightfold's on any host
    elif not (known and lower == ["careers"]):
        return None
    if job_id is not None and not _EIGHTFOLD_ID_RE.fullmatch(job_id):
        return None

    slug = eightfold_slug(host, domain)
    if slug is None:
        return None
    search = {} if job_id else {
        k: v for k, v in query.items()
        if v and (k in _EIGHTFOLD_SEARCH_PARAMS or k.startswith("filter_"))
    }
    return EightfoldAddress(host, domain, slug, job_id, search)


def eightfold_slug(host: str, domain: str | None) -> str | None:
    """The company an Eightfold site belongs to, by name.

    "careers.qualcomm.com" -> "qualcomm", "explore.jobs.netflix.net" -> "netflix".
    On Eightfold's own hosts the `domain` names it ("app.eightfold.ai" with
    domain=qualcomm.com -> "qualcomm"), else the tenant ("qualcomm.eightfold.ai").
    Any other host's `domain` parameter is ignored, so a job's URL with or
    without it lands on one company.
    """
    if host.endswith(_EIGHTFOLD_HOST_SUFFIX):
        if domain:
            name = _company_label(domain)
        else:
            tenant = host.removesuffix(_EIGHTFOLD_HOST_SUFFIX).split(".")[-1]
            name = None if tenant in ("app", "www", "") else tenant
    else:
        name = _company_label(host)
    slug = slugify_company(name or "")
    return slug or None


def _company_label(host: str) -> str | None:
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return None
    if labels[-2] in _SECOND_LEVEL_LABELS and len(labels) >= 3:
        return labels[-3]
    return labels[-2]


def _parse_dover(segments: list[str], query: dict[str, str]) -> tuple[str, str] | None:
    # app.dover.com/apply/{Company Name}/{job_id}
    if len(segments) == 3 and segments[0].lower() == "apply":
        slug = slugify_company(segments[1])
        if slug and _UUID_RE.fullmatch(segments[2]):
            return slug, segments[2].lower()
    return None


def _parse_rippling(
    segments: list[str], query: dict[str, str]
) -> tuple[str, str | None] | None:
    # ats.rippling.com/[{locale}/]{slug}/jobs/{job_id}, or the board without a job id.
    # "hp/jobs/..." is the company hp, not a locale: a locale comes before a slug.
    if len(segments) >= 2 and _LOCALE_RE.fullmatch(segments[0]) and segments[1].lower() != "jobs":
        segments = segments[1:]
    if len(segments) == 3 and segments[1].lower() == "jobs" and _UUID_RE.fullmatch(segments[2]):
        return segments[0], segments[2].lower()
    if len(segments) in (1, 2) and segments[1:] in ([], ["jobs"]):
        return segments[0], None
    return None


def _parse_amazon(
    segments: list[str], query: dict[str, str]
) -> tuple[str, str | None] | None:
    # amazon.jobs/[{locale}/]jobs/{job_id}[/{title-slug}]. The id is a number;
    # the title after it is decoration and the tracking query is ignored.
    if segments and _LOCALE_RE.fullmatch(segments[0]):
        segments = segments[1:]
    if (
        len(segments) in (2, 3)
        and segments[0].lower() == "jobs"
        and _AMAZON_JOB_ID_RE.fullmatch(segments[1])
    ):
        return "amazon", segments[1]
    # A team is a board: its page, or a search narrowed to it.
    team = None
    if len(segments) == 2 and segments[0].lower() == "teams":
        team = segments[1]
    elif [s.lower() for s in segments] == ["search"]:
        team = query.get("team_category[]")
    slug = _amazon_team_slug(team)
    return (slug, None) if slug else None


def _amazon_team_slug(team: str | None) -> str | None:
    """"amazon-robotics" stays; "project-kuiper" -> "amazon-project-kuiper".

    Mirrors fetchers.amazon.team_slug, which names the same team from a job.
    """
    name = (team or "").strip().lower().removeprefix("team-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return None
    return name if name.startswith("amazon") else f"amazon-{name}"


def _parse_smartrecruiters(
    segments: list[str], query: dict[str, str]
) -> tuple[str, str | None] | None:
    # The API's own address, as its documentation writes it:
    # v1/companies/{Company}/postings[/{job_id}]
    if len(segments) in (4, 5) and [s.lower() for s in segments[:2]] == ["v1", "companies"]:
        if segments[3].lower() != "postings":
            return None
        segments = [segments[2], *segments[4:]]
    # {Company}/{job_id}[-{title-slug}], or the company alone for its board.
    # The identifier is case-insensitive, so the slug is its lower-case form.
    if not segments or not _SR_COMPANY_RE.fullmatch(segments[0]):
        return None
    slug = segments[0].lower()
    if len(segments) == 1:
        return slug, None
    job = _SR_JOB_RE.fullmatch(segments[1]) if len(segments) == 2 else None
    return (slug, job.group(1)) if job else None


def _parse_workable(
    segments: list[str], query: dict[str, str]
) -> tuple[str | None, str | None] | None:
    lower = [s.lower() for s in segments]
    # The API's own addresses, as its documentation writes them.
    if lower[:4] == ["api", "v1", "widget", "accounts"] and len(segments) == 5:
        return _workable_account(segments[4]), None
    if len(segments) == 6 and lower[0] == "api" and lower[2] == "accounts" and lower[4] == "jobs":
        return _workable_job(_workable_account(segments[3]), segments[5])
    if lower[:1] == ["api"]:
        return None
    # jobs.workable.com: the job board, where a job has a view id and no account.
    if len(segments) in (2, 3) and lower[0] == "view" and _WORKABLE_VIEW_RE.fullmatch(segments[1]):
        return None, segments[1]
    if len(segments) == 3 and lower[0] == "company" and lower[2].startswith("jobs-at-"):
        account = _workable_account(segments[2][len("jobs-at-"):])
        return (account, None) if account else None
    # apply.workable.com: the short link, a job, or the account's board.
    if len(segments) == 2 and lower[0] == "j":
        return _workable_job(None, segments[1], needs_account=False)
    if len(segments) == 3 and lower[1] == "j":
        return _workable_job(_workable_account(segments[0]), segments[2])
    if len(segments) == 1 and lower[0] not in ("view", "company", "j", "search"):
        account = _workable_account(segments[0])
        return (account, None) if account else None
    return None


def _workable_account(text: str) -> str | None:
    return text.lower() if _WORKABLE_ACCOUNT_RE.fullmatch(text) else None


def _workable_job(
    account: str | None, shortcode: str, needs_account: bool = True
) -> tuple[str | None, str] | None:
    if not _WORKABLE_SHORTCODE_RE.fullmatch(shortcode) or (needs_account and account is None):
        return None
    return account, shortcode.upper()


_PATH_PARSERS = {
    Platform.WORKABLE: _parse_workable,
    Platform.SMARTRECRUITERS: _parse_smartrecruiters,
    Platform.AMAZON: _parse_amazon,
    Platform.DOVER: _parse_dover,
    Platform.RIPPLING: _parse_rippling,
    Platform.ASHBY: _parse_ashby,
    Platform.GREENHOUSE: _parse_greenhouse,
    Platform.LEVER: _parse_lever,
}


def _unsupported(url: str) -> ParsedUrl:
    return ParsedUrl(
        platform=Platform.UNSUPPORTED, slug=None, job_id=None, url=url.strip()
    )
