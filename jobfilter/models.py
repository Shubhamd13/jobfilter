"""Normalized data shared across url_parser, fetchers and classifiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Platform(StrEnum):
    """Job board a URL belongs to.

    StrEnum so callers can compare against plain strings (`p == "ashby"`)
    and so the value serializes straight into JSONL output.
    """

    ASHBY = "ashby"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    # Fetched like the three above, and also read by the browser extension.
    WORKDAY = "workday"
    DOVER = "dover"
    RIPPLING = "rippling"
    SMARTRECRUITERS = "smartrecruiters"
    WORKABLE = "workable"
    # Hosted on the employer's own domain (careers.qualcomm.com).
    EIGHTFOLD = "eightfold"
    # One employer's own careers site rather than an ATS: job URLs only, no board.
    AMAZON = "amazon"
    # Seen in the browser extension rather than fetched. No fetcher exists for
    # these and url_parser never returns them: the extension reads the page
    # itself and posts the text to /api/browsed.
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    WEB = "web"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    """A job URL broken into the parts a fetcher needs.

    For `UNSUPPORTED` both `slug` and `job_id` are None; `url` always keeps the
    caller's original string so unsupported inputs can still be reported.
    A Greenhouse URL may have `slug=None` (e.g. `company.com/careers?gh_jid=123`);
    its fetcher looks the slug up from the job id.
    A Workable short link or job-board link has `slug=None` too; its fetcher
    finds the account. A Workday board URL has `job_id=None`: it stands for
    every job on the board.
    """

    platform: Platform
    slug: str | None
    job_id: str | None
    url: str

    @property
    def supported(self) -> bool:
        return self.platform is not Platform.UNSUPPORTED

    @property
    def is_board(self) -> bool:
        """A whole company board rather than one job."""
        return self.supported and self.job_id is None


@dataclass(frozen=True, slots=True)
class Posting:
    """A job posting normalized across platforms.

    `description_text` is plain text with HTML already stripped -- it is the only
    field a classifier reads. `raw` keeps the platform's original JSON so later
    phases can reach for fields we haven't normalized (Greenhouse's
    `company_name`, Lever's `categories`, Ashby's `descriptionHtml`).
    """

    platform: Platform
    company_slug: str
    job_id: str
    title: str
    location: str | None
    url: str
    description_text: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # Listing signals in the site's own words ("Reposted 2 weeks ago", "Over
    # 100 applicants"). Only job boards show them -- the extension reads them
    # off LinkedIn -- so ATS fetchers leave them None.
    posted_ago: str | None = None
    applicants: str | None = None


class Label(StrEnum):
    """Classification outcome for a posting.

    `INFO_NO_SPONSORSHIP` is deliberately not a restriction: "we don't sponsor
    visas" tells you about paperwork, not about who is allowed to apply.
    """

    RESTRICTED_CITIZEN = "RESTRICTED_CITIZEN"
    RESTRICTED_US_PERSON = "RESTRICTED_US_PERSON"
    RESTRICTED_CLEARANCE = "RESTRICTED_CLEARANCE"
    INFO_NO_SPONSORSHIP = "INFO_NO_SPONSORSHIP"
    UNCLEAR = "UNCLEAR"
    OPEN = "OPEN"

    @property
    def is_restriction(self) -> bool:
        return self in _RESTRICTIONS


_RESTRICTIONS = frozenset(
    {
        Label.RESTRICTED_CITIZEN,
        Label.RESTRICTED_US_PERSON,
        Label.RESTRICTED_CLEARANCE,
    }
)


@dataclass(frozen=True, slots=True)
class Decision:
    """Why a posting got its label.

    `evidence` is the sentence that triggered the decision, verbatim, so a human
    can check the call without reopening the posting. Rule matches are exact, so
    `confidence` is always 1.0; the model classifiers in Phase 2b will vary it.
    """

    label: Label
    confidence: float
    evidence: str | None
    matched_rule: str | None
    classifier_name: str
    # Every rule that fired on the winning sentence, primary one first. Not in
    # the CLAUDE.md field list, but it is what makes a surprising label
    # debuggable without re-running the classifier.
    matched_rules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON form served by /api/classify."""
        return {
            "label": str(self.label),
            "confidence": self.confidence,
            "evidence": self.evidence,
            "matched_rule": self.matched_rule,
            "matched_rules": list(self.matched_rules),
            "classifier_name": self.classifier_name,
        }


class Origin(StrEnum):
    """Did we check this posting because you asked, or because we found it?

    `DISCOVERED` means it came from expanding a company's board: you gave one
    URL, the board response held the rest. `BROWSED` means the browser
    extension saw it while you were looking at it.
    """

    REQUESTED = "requested"
    DISCOVERED = "discovered"
    BROWSED = "browsed"


class AppStatus(StrEnum):
    """What you did about a posting after reading it. Unmarked postings have none."""

    APPLIED = "applied"
    SKIPPED = "skipped"
    MAYBE = "maybe"


class Outcome(StrEnum):
    """A step an application reached, in the order they usually come.

    An application you have heard nothing about has none.
    """

    OA = "oa"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"


class Change(StrEnum):
    """What a board refresh found different about one posting.

    `CLOSED` means the board no longer lists the job; the posting is kept.
    `EDITED` is a changed description, `RELABELED` a changed verdict -- an
    edit usually leaves the label alone, so they are counted apart.
    """

    NEW = "new"
    EDITED = "edited"
    RELABELED = "relabeled"
    CLOSED = "closed"
    REOPENED = "reopened"


@dataclass(frozen=True, slots=True)
class CompanyRefresh:
    """One company's board, re-read: what it lists now and what changed.

    `status` is "error" when the board itself could not be read; nothing is
    marked closed then. `failed` counts jobs the board lists that could not
    be fetched -- they are still listed, just not checked.
    """

    platform: Platform
    slug: str
    name: str | None
    status: str
    listed: int = 0
    changes: dict[Change, int] = field(default_factory=dict)
    failed: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def changed(self) -> int:
        return sum(self.changes.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": str(self.platform),
            "slug": self.slug,
            "name": self.name,
            "status": self.status,
            "listed": self.listed,
            "changes": {str(kind): n for kind, n in self.changes.items() if n},
            "changed": self.changed,
            "failed": self.failed,
            "error": self.error,
        }


class Status(StrEnum):
    """Did we get far enough to actually classify this URL?

    Kept separate from `Label` so a fetch failure is never confused with a
    genuine UNCLEAR: both need a human, but for different reasons.
    """

    OK = "ok"
    UNSUPPORTED = "unsupported"
    FETCH_ERROR = "fetch_error"


@dataclass(frozen=True, slots=True)
class Result:
    """One row of output: everything known about one input URL.

    Every job URL produces exactly one Result (a board URL, one per job on
    the board), including URLs we could not parse or fetch -- CLAUDE.md's "never silently drop postings". Failures carry
    `status != OK` and `label = UNCLEAR`.
    """

    url: str
    status: Status
    platform: Platform
    company_slug: str | None
    job_id: str | None
    title: str | None
    location: str | None
    label: Label
    confidence: float | None
    evidence: str | None
    matched_rule: str | None
    matched_rules: tuple[str, ...]
    classifier_name: str | None
    error: str | None
    error_type: str | None
    origin: Origin = Origin.REQUESTED
    # The posting this came from, when the fetch succeeded. Excluded from
    # to_dict and from equality: it is passed to the storage layer, which needs
    # description_text and raw, and is not part of the JSONL row.
    posting: "Posting | None" = field(default=None, repr=False, compare=False)

    @property
    def company(self) -> str:
        """Grouping key for the per-company summary."""
        return self.company_slug or "(unparsed)"

    def to_dict(self) -> dict[str, Any]:
        """Flat JSON-serializable form, one per JSONL line."""
        return {
            "url": self.url,
            "status": str(self.status),
            "origin": str(self.origin),
            "platform": str(self.platform),
            "company_slug": self.company_slug,
            "job_id": self.job_id,
            "title": self.title,
            "location": self.location,
            "label": str(self.label),
            "confidence": self.confidence,
            "evidence": self.evidence,
            "matched_rule": self.matched_rule,
            "matched_rules": list(self.matched_rules),
            "classifier_name": self.classifier_name,
            "error": self.error,
            "error_type": self.error_type,
        }
