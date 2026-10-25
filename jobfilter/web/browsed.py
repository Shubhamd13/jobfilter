"""Turn what the browser extension saw into a Posting.

The extension reads LinkedIn / Indeed / Workday / any page itself -- there is no API to
fetch from -- and posts the extracted text here. Everything in that body came
from a web page, so it is validated, size-capped, and never trusted for the
verdict: the server classifies the text again with its own classifier.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from ..models import Platform, Posting

SITES = {
    "linkedin": Platform.LINKEDIN,
    "indeed": Platform.INDEED,
    "workday": Platform.WORKDAY,
    "web": Platform.WEB,
}
MAX_TEXT_CHARS = 200_000
MAX_FIELD_CHARS = 500
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")

# Names that hide the employer rather than name it. Two "Stealth Startup"
# postings are almost never the same company, so these never group together.
# Matched against the slugified name.
_PLACEHOLDER_NAME = re.compile(
    r"^(?:"
    r"stealth(?:-.*)?"  # stealth, stealth-startup, stealth-mode-ai-company, ...
    r"|(?:company-)?confidential(?:-(?:company|employer|client))?"
    r"|undisclosed(?:-(?:company|employer|client))?"
    r"|(?:private|anonymous|unnamed)-(?:company|employer|client)"
    r"|anonymous|unknown|n-a|na|none"
    r")$"
)


class BadPosting(ValueError):
    """The request body cannot be turned into a Posting. Becomes a 400."""


def posting_from_browsed(body: Mapping[str, Any]) -> Posting:
    """Validate an extension payload and normalize it into a Posting."""
    site = _field(body, "site")
    if site not in SITES:
        raise BadPosting(f"site must be one of {sorted(SITES)}, got {site!r}")
    platform = SITES[site]

    url = _field(body, "url")
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BadPosting("url must be an absolute http(s) URL")

    text = body.get("text")
    if not isinstance(text, str):
        raise BadPosting("text must be a string")
    if len(text) > MAX_TEXT_CHARS:
        raise BadPosting(f"text is longer than {MAX_TEXT_CHARS} characters")

    company = _field(body, "company")
    job_id = _field(body, "job_id")
    if platform is not Platform.WEB and not job_id:
        raise BadPosting(f"job_id is required for {site}")

    # A generic page has no job id of its own; its URL is its identity.
    job_id = job_id or url_id(url)
    return Posting(
        platform=platform,
        company_slug=company_slug(platform, company, parsed.hostname or "", job_id),
        job_id=job_id,
        title=_field(body, "title") or "(untitled)",
        location=_field(body, "location"),
        posted_ago=_field(body, "posted_ago"),
        applicants=_field(body, "applicants"),
        url=url,
        description_text=text,
        raw={
            "source": "extension",
            "site": site,
            "company_name": company,
            "extraction": body.get("extraction")
            if isinstance(body.get("extraction"), dict)
            else None,
        },
    )


def company_slug(
    platform: Platform, company: str | None, hostname: str, job_id: str
) -> str:
    """LinkedIn and Indeed have no slug in the URL, so derive one from the name.

    A missing or placeholder name ("Stealth Startup", "Confidential") says
    nothing about who the employer is, so it gets a group of its own per
    posting -- `stealth-startup~4448673866` -- rather than pooling unrelated
    employers into one company in the per-company summary.

    A generic page groups by host instead: every posting on careers.acme.com
    belongs together even when the page never states the company name.

    Workday's job ids are only unique within one tenant, so the tenant in the
    hostname (nvidia.wd5.myworkdayjobs.com) is the company, whatever name the
    page text claims.
    """
    if platform is Platform.WEB:
        return hostname.lower().removeprefix("www.") or f"unknown~{job_id}"
    if platform is Platform.WORKDAY:
        return hostname.lower().split(".")[0] or f"unknown~{job_id}"
    slug = _SLUG_JUNK.sub("-", (company or "").lower()).strip("-")
    if not slug or is_placeholder_company(company):
        return f"{slug or 'unknown'}~{job_id}"
    return slug


def is_placeholder_company(company: str | None) -> bool:
    """True for a name that hides the employer: "Stealth Startup", "Confidential"."""
    slug = _SLUG_JUNK.sub("-", (company or "").lower()).strip("-")
    return not slug or bool(_PLACEHOLDER_NAME.fullmatch(slug))


def url_id(url: str) -> str:
    """Stable id for a page with no job id: its URL minus query and fragment."""
    parsed = urlparse(url)
    canonical = f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _field(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, (str, int)):
        raise BadPosting(f"{key} must be a string")
    text = str(value).strip()
    if len(text) > MAX_FIELD_CHARS:
        raise BadPosting(f"{key} is longer than {MAX_FIELD_CHARS} characters")
    return text or None
