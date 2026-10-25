"""JSON API, independent of the HTTP server.

Routing and payload building live here rather than in the request handler, so
the whole API can be tested by calling `Api.handle(...)` with no socket.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from ..classifiers import Classifier, RuleClassifier
from ..fetchers import FetcherRegistry, HttpClient
from ..location import Region, describe_place
from ..models import AppStatus, CompanyRefresh, Label, Origin, Outcome, Platform, Posting, Result
from ..roles import Stage, Verdict, screen_store
from ..storage import (
    CALENDAR_KINDS,
    CLOSING_SOON_DAYS,
    NO_RESUME,
    NO_STATUS,
    UNREAD,
    UNSCREENED,
    BoardCompany,
    NotApplied,
    ResumeInUse,
    Store,
)
from .browsed import BadPosting, posting_from_browsed
from .process_job import ProcessJob, Screener, StoreReader
from .refresh_job import Progress, Refresher, RefreshJob, idle_state

Checker = Callable[[Sequence[str], bool], list[Result]]

# A textarea holds whitespace-separated URLs; '#' starts a comment line.
_COMMENT = re.compile(r"^\s*#")
MAX_URLS_PER_REQUEST = 200
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000

MAX_RESUME_BYTES = 5_000_000
MAX_FILENAME_LENGTH = 200
# What a resume may be: extension -> (content type, how such a file starts).
# The type served back comes from here, never from what the upload claimed.
RESUME_TYPES: dict[str, tuple[str, bytes]] = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"PK\x03\x04",
    ),
}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """JSON (`payload`), or a file when `body` is set."""

    status: int
    payload: dict[str, Any]
    body: bytes | None = None
    content_type: str | None = None
    filename: str | None = None


def parse_url_blob(text: str) -> list[str]:
    """Split a pasted block into URLs, ignoring blanks and comment lines."""
    urls: list[str] = []
    for line in (text or "").splitlines():
        if _COMMENT.match(line):
            continue
        urls.extend(line.split())
    return urls


def make_checker(
    classifier: RuleClassifier | None = None, timeout: float = 10.0
) -> Checker:
    """Build the real pipeline. The classifier is loaded once and reused.

    A fresh FetcherRegistry per request gives each submission its own Ashby
    board cache -- the same per-run semantics the CLI has.
    """
    from ..cli import expand_companies, run

    resolved = classifier or RuleClassifier()

    def check(urls: Sequence[str], expand: bool = False) -> list[Result]:
        with FetcherRegistry(HttpClient(timeout=timeout)) as registry:
            results = run(urls, registry, resolved)
            if expand:
                results += expand_companies(results, registry, resolved)
            return results

    return check


def make_refresher(
    store: Store, classifier: Classifier | None = None, timeout: float = 10.0
) -> Refresher:
    """Build the real board refresh. One FetcherRegistry per refresh, as per run."""
    from ..refresh import refresh_boards

    resolved = classifier or RuleClassifier()

    def refresh(
        boards: Sequence[BoardCompany], deep: bool, progress: Progress
    ) -> list[CompanyRefresh]:
        with FetcherRegistry(HttpClient(timeout=timeout)) as registry:
            return refresh_boards(
                store, registry, resolved, boards, deep=deep, progress=progress
            )

    return refresh


def make_store_reader(model: str | None = None, timeout: float | None = None) -> StoreReader:
    """Build the real reading: a local model under Ollama, the profile loaded fresh.

    Raises ValueError, when run, if the profile has no "fit" section: without
    one a reading could not become a verdict.
    """
    from ..roles import OllamaReader, Profile, RoleRules, read_store
    from ..roles.reader import DEFAULT_TIMEOUT

    def read(store: Store, progress: Any) -> Any:
        profile = Profile.load()
        if profile.fit is None:
            raise ValueError('the profile has no "fit" section, so no verdict can be given')
        reader = OllamaReader(
            HttpClient(timeout=timeout or DEFAULT_TIMEOUT),
            model=model,
            degree=profile.fit.highest_degree,
        )
        try:
            return read_store(
                store, reader, rules=RoleRules.load(), profile=profile, progress=progress
            )
        finally:
            reader.close()

    return read


class Api:
    """Every endpoint the page and the browser extension call."""

    def __init__(
        self,
        store: Store,
        checker: Checker,
        classifier: Classifier | None = None,
        refresher: Refresher | None = None,
        screener: Screener = screen_store,
        reader: StoreReader | None = None,
    ) -> None:
        self.store = store
        self.checker = checker
        self.screener = screener
        # Screens, then reads with `reader`. Without one (tests, or a server
        # started with no model) it screens only.
        self.process_job = ProcessJob(store, screener, reader)
        # None in tests that never refresh: POST /api/refresh then answers 503.
        self.refresh_job = RefreshJob(refresher) if refresher else None
        # What /api/classify and /api/browsed run. Today the rule classifier;
        # in Phase 2b whatever the server is configured with, which is how the
        # extension reaches Ollama without changing.
        self.classifier = classifier or RuleClassifier()

    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]] | None = None,
        body: Mapping[str, Any] | None = None,
        raw: bytes | None = None,
    ) -> ApiResponse:
        """`body` is a JSON request body; `raw` the bytes of an uploaded file."""
        query = query or {}

        if method == "GET":
            if path == "/api/stats":
                return self.stats()
            if path == "/api/postings":
                return self.postings(query)
            if path == "/api/companies":
                return self.companies(query)
            if path == "/api/failures":
                return self.failures()
            if path == "/api/health":
                return self.health()
            if path == "/api/place":
                return self.place(query)
            if path == "/api/refresh":
                return self.refresh_state()
            if path == "/api/process":
                return self.process_state()
            if path == "/api/resumes":
                return self.resumes()
            if path == "/api/calendar":
                return self.calendar(query)
            match = re.fullmatch(r"/api/postings/(\d+)", path)
            if match:
                return self.posting(int(match.group(1)))
            match = re.fullmatch(r"/api/resumes/(\d+)/file", path)
            if match:
                return self.resume_file(int(match.group(1)))

        if method == "POST":
            match = re.fullmatch(r"/api/postings/(\d+)/application", path)
            if match:
                return self.application(int(match.group(1)), body or {})
            match = re.fullmatch(r"/api/postings/(\d+)/outcome", path)
            if match:
                return self.outcome(int(match.group(1)), body or {})
            if path == "/api/resumes":
                return self.upload_resume(query, raw)
            match = re.fullmatch(r"/api/resumes/(\d+)", path)
            if match:
                return self.edit_resume(int(match.group(1)), body or {})
            if path == "/api/screen":
                return self.screen()
            if path == "/api/process":
                return self.process()

        if method == "POST" and path == "/api/check":
            return self.check(body or {})
        if method == "POST" and path == "/api/classify":
            return self.classify(body or {})
        if method == "POST" and path == "/api/browsed":
            return self.browsed(body or {})
        if method == "POST" and path == "/api/refresh":
            return self.refresh(body or {})

        return ApiResponse(404, {"error": f"no route for {method} {path}"})

    # -- endpoints --------------------------------------------------------

    def stats(self) -> ApiResponse:
        return ApiResponse(
            200,
            {
                "stats": self.store.stats(),
                # `refreshable`: the company has a board to re-read (LinkedIn
                # and Indeed companies do not). `refresh` is its latest one.
                "companies": [
                    {**row, "refreshable": row["platform"] in self.board_platforms}
                    for row in self.store.company_summary()
                ],
                "labels": [str(label) for label in Label],
                "regions": [str(region) for region in Region],
                "places": self.store.location_places(),
                # In the order the page lists them: what to act on first.
                "app_statuses": [NO_STATUS, AppStatus.MAYBE, AppStatus.APPLIED, AppStatus.SKIPPED],
                "outcomes": [str(stage) for stage in Outcome],
                "stages": [*reversed(Stage), UNSCREENED],
                "verdicts": [
                    Verdict.BEST, Verdict.FIT, Verdict.STRETCH, Verdict.UNKNOWN, UNREAD, Verdict.SKIP,
                ],
                # How near an apply-by date makes a posting "closing soon".
                "closing_soon_days": CLOSING_SOON_DAYS,
            },
        )

    def calendar(self, query: Mapping[str, list[str]]) -> ApiResponse:
        """Postings per day between `from` and `to` (YYYY-MM-DD, inclusive).

        `kind=applied` counts postings marked applied, on the day you marked
        them; `kind=added` counts postings on the day first seen. Every
        /api/postings filter narrows the counts too. `days` maps a day to its
        count and leaves out days with none. A day's postings are
        /api/postings?applied_on=DAY (or added_on=DAY) with the same filters.
        """
        kind = _one(query, "kind")
        start, end = _day(query, "from"), _day(query, "to")
        if kind not in CALENDAR_KINDS or start is None or end is None or start > end:
            return ApiResponse(
                400, {"error": f"give kind ({' or '.join(CALENDAR_KINDS)}) and from <= to as YYYY-MM-DD"}
            )
        days = self.store.day_counts(kind, start, end, **_filters(query))
        return ApiResponse(200, {"kind": kind, "from": start, "to": end, "days": days})

    def postings(self, query: Mapping[str, list[str]]) -> ApiResponse:
        """One page of postings, plus what the pager and filter pane need.

        `label`, `company`, `exclude`, `region`, `place` and `exclude_place`
        may repeat. `changed=1` keeps only postings their company's latest
        board refresh found different, and `closing=1` only open postings
        whose apply-by date is near, soonest first; `posted=7` only postings
        that went up in the last 7 days. `order` is newest (last checked,
        the default), posted / posted_asc (posting date, latest or earliest
        first) or deadline (closing soon first). `app` (applied / skipped / maybe / none)
        `stage` (a funnel stage, or unscreened) and `verdict` (fit / stretch /
        skip / unknown, or unread) may repeat too, and so may `outcome` (oa /
        interview / offer / rejected, or waiting) and `resume` (a resume's
        id, or none). `total` counts every row the filters match; each of
        `label_counts`, `region_counts`, `app_counts`, `stage_counts`,
        `verdict_counts` and `outcome_counts` applies every filter but its
        own. `outcome_counts` counts applied postings only.
        """
        filters = _filters(query)
        limit = _int(query, "limit", DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
        offset = _int(query, "offset", 0)
        asked = _one(query, "order")
        order = "deadline" if filters["closing"] else asked if asked in ORDERS else "newest"
        return ApiResponse(
            200,
            {
                "postings": self.store.postings(
                    limit=limit, offset=offset, order=order, **filters
                ),
                "total": self.store.count_postings(**filters),
                "limit": limit,
                "offset": offset,
                "label_counts": self.store.filtered_label_counts(**filters),
                "region_counts": self.store.filtered_region_counts(**filters),
                "app_counts": self.store.filtered_app_counts(**filters),
                "stage_counts": self.store.filtered_stage_counts(**filters),
                "verdict_counts": self.store.filtered_verdict_counts(**filters),
                "outcome_counts": self.store.filtered_outcome_counts(**filters),
            },
        )

    def companies(self, query: Mapping[str, list[str]]) -> ApiResponse:
        """The per-company table, counted under the same filters as /api/postings.

        Every company is listed whatever the filters; `all_total` is its
        unfiltered total. The company filters (`company`, `exclude`) do not
        apply here. `refreshable`: the company has a board to re-read
        (LinkedIn and Indeed companies do not). `refresh` is its latest one.
        """
        return ApiResponse(
            200,
            {
                "companies": [
                    {**row, "refreshable": row["platform"] in self.board_platforms}
                    for row in self.store.company_summary(**_filters(query))
                ]
            },
        )

    def place(self, query: Mapping[str, list[str]]) -> ApiResponse:
        """How a typed place will be read, and how many postings it matches.

        Lets the place finder say "3,004 jobs · every US location" for "usa"
        without the page re-implementing the location rules.
        """
        wanted = _one(query, "q")
        if wanted is None:
            return ApiResponse(400, {"error": "q is required"})
        return ApiResponse(
            200,
            {
                "place": wanted,
                "meaning": describe_place(wanted),
                "total": self.store.count_postings(place=[wanted]),
            },
        )

    def posting(self, posting_id: int) -> ApiResponse:
        detail = self.store.posting(posting_id)
        if detail is None:
            return ApiResponse(404, {"error": f"no posting {posting_id}"})
        return ApiResponse(200, {"posting": detail})

    def failures(self) -> ApiResponse:
        return ApiResponse(200, {"failures": self.store.failures()})

    def application(self, posting_id: int, body: Mapping[str, Any]) -> ApiResponse:
        """Mark a posting applied / skipped / maybe; a null status clears the mark.

        `resume_id` attaches the resume you sent, null detaches it.
        """
        status = body.get("status")
        note = body.get("note")
        resume_id = body.get("resume_id")
        if status is not None and status not in {str(s) for s in AppStatus}:
            allowed = ", ".join(str(s) for s in AppStatus)
            return ApiResponse(400, {"error": f"status must be one of {allowed}, or null"})
        if note is not None and not isinstance(note, str):
            return ApiResponse(400, {"error": "note must be a string"})
        if resume_id is not None and (isinstance(resume_id, bool) or not isinstance(resume_id, int)):
            return ApiResponse(400, {"error": "resume_id must be a resume's id, or null"})
        # No "note" key leaves the note alone, so a quick re-mark keeps it;
        # the same goes for the resume.
        given = {key: body[key] for key in ("note", "resume_id") if key in body}
        try:
            found = self.store.set_application(posting_id, status, **given)
        except ValueError as exc:  # the status is checked above: an unknown resume
            return ApiResponse(400, {"error": str(exc)})
        if not found:
            return ApiResponse(404, {"error": f"no posting {posting_id}"})
        return ApiResponse(200, {"posting_id": posting_id, "status": status})

    def outcome(self, posting_id: int, body: Mapping[str, Any]) -> ApiResponse:
        """Record a step an application reached; `"on": false` takes it back."""
        stage = body.get("stage")
        on = body.get("on", True)
        if stage not in {str(s) for s in Outcome}:
            allowed = ", ".join(str(s) for s in Outcome)
            return ApiResponse(400, {"error": f"stage must be one of {allowed}"})
        if not isinstance(on, bool):
            return ApiResponse(400, {"error": "on must be true or false"})
        try:
            found = self.store.set_outcome(posting_id, stage, on)
        except NotApplied:
            return ApiResponse(409, {"error": "mark the posting Applied first"})
        if not found:
            return ApiResponse(404, {"error": f"no posting {posting_id}"})
        return ApiResponse(200, {"posting_id": posting_id, "stage": stage, "on": on})

    def resumes(self) -> ApiResponse:
        """Every resume, and what came of the applications each one went to.

        `results` is keyed by resume id, "none" for applications sent with no
        resume attached; a resume attached to no application is not in it.
        """
        results = self.store.resume_results()
        return ApiResponse(
            200,
            {
                "resumes": self.store.resumes(),
                "results": {
                    NO_RESUME if resume_id is None else str(resume_id): counts
                    for resume_id, counts in results.items()
                },
            },
        )

    def upload_resume(self, query: Mapping[str, list[str]], raw: bytes | None) -> ApiResponse:
        """Keep an uploaded resume. `name` is its file name; the body is the file.

        201 for a new resume, 200 with `reused` when the same file was
        uploaded before: it is then the resume already stored.
        """
        if raw is None:
            return ApiResponse(
                400, {"error": "send the file as the request body, as application/octet-stream"}
            )
        filename = _filename(_one(query, "name") or "")
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix not in RESUME_TYPES:
            return ApiResponse(400, {"error": "a resume must be a .pdf or .docx file"})
        if not raw:
            return ApiResponse(400, {"error": f"{filename} is empty"})
        if len(raw) > MAX_RESUME_BYTES:
            limit = MAX_RESUME_BYTES // 1_000_000
            return ApiResponse(400, {"error": f"{filename} is larger than {limit} MB"})
        content_type, opening = RESUME_TYPES[suffix]
        if not raw.startswith(opening):
            return ApiResponse(
                400, {"error": f"{filename} is not a {suffix[1:].upper()} file inside"}
            )
        resume, reused = self.store.add_resume(
            PurePosixPath(filename).stem, filename, content_type, raw
        )
        return ApiResponse(200 if reused else 201, {"resume": resume, "reused": reused})

    def resume_file(self, resume_id: int) -> ApiResponse:
        found = self.store.resume_file(resume_id)
        if found is None:
            return ApiResponse(404, {"error": f"no resume {resume_id}"})
        return ApiResponse(
            200, {}, body=found["data"], content_type=found["content_type"], filename=found["filename"]
        )

    def edit_resume(self, resume_id: int, body: Mapping[str, Any]) -> ApiResponse:
        """`{"name": ...}` renames a resume; `{"delete": true}` deletes an unused one."""
        missing = ApiResponse(404, {"error": f"no resume {resume_id}"})
        if body.get("delete") is True:
            try:
                found = self.store.delete_resume(resume_id)
            except ResumeInUse:
                return ApiResponse(
                    409, {"error": "that resume is attached to an application: detach it first"}
                )
            return ApiResponse(200, {"resume_id": resume_id, "deleted": True}) if found else missing
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return ApiResponse(400, {"error": 'give a "name", or "delete": true'})
        if not self.store.rename_resume(resume_id, name):
            return missing
        return ApiResponse(200, {"resume_id": resume_id, "name": name.strip()})

    def screen(self) -> ApiResponse:
        """Run the role funnel over every stored posting and store the result."""
        try:
            stages = self.screener(self.store)
        except (OSError, ValueError, KeyError) as exc:
            # A missing or malformed role_rules.json / profile.json.
            return ApiResponse(500, {"error": f"screening failed: {exc}"})
        return ApiResponse(200, {"stages": stages, "screened": sum(stages.values())})

    def check(self, body: Mapping[str, Any]) -> ApiResponse:
        urls = parse_url_blob(str(body.get("urls", "")))
        if not urls:
            return ApiResponse(400, {"error": "no URLs found in the submitted text"})
        if len(urls) > MAX_URLS_PER_REQUEST:
            return ApiResponse(
                400,
                {
                    "error": (
                        f"{len(urls)} URLs submitted; the limit is "
                        f"{MAX_URLS_PER_REQUEST} per request"
                    )
                },
            )

        expand = bool(body.get("expand"))
        results = self.checker(urls, expand)
        run_id = self.store.record_run(
            source="web+expand" if expand else "web", results=results
        )
        return ApiResponse(
            200,
            {
                "run_id": run_id,
                "requested": len(urls),
                "checked": len(results),
                "discovered": sum(
                    1 for r in results if r.origin is Origin.DISCOVERED
                ),
                "results": [r.to_dict() for r in results],
            },
        )


    @cached_property
    def board_platforms(self) -> frozenset[Platform]:
        """Platforms whose companies can be refreshed. Asked of a registry once."""
        with FetcherRegistry() as registry:
            return frozenset(registry.board_platforms())

    def refresh_state(self) -> ApiResponse:
        """Progress of the running refresh, or the outcome of the last one."""
        state = self.refresh_job.state() if self.refresh_job else idle_state()
        return ApiResponse(200, {"refresh": state})

    def refresh(self, body: Mapping[str, Any]) -> ApiResponse:
        """Start re-reading stored companies' boards in the background.

        `companies` narrows it to some (`[{platform, slug}]`); without it every
        stored company with a board is refreshed. 202 once started; 409 while
        another refresh runs -- its progress comes back either way.
        """
        if self.refresh_job is None:
            return ApiResponse(503, {"error": "refresh is not available on this server"})

        boards = self.store.board_companies(self.board_platforms)
        wanted = body.get("companies")
        if wanted is not None:
            try:
                pairs = _company_pairs(wanted)
            except ValueError as exc:
                return ApiResponse(400, {"error": str(exc)})
            known = {(str(board.platform), board.slug) for board in boards}
            unknown = [f"{platform}:{slug}" for platform, slug in pairs if (platform, slug) not in known]
            if unknown:
                return ApiResponse(
                    400,
                    {"error": f"no stored company with a board: {', '.join(unknown)}"},
                )
            boards = [b for b in boards if (str(b.platform), b.slug) in pairs]
        if not boards:
            return ApiResponse(400, {"error": "no stored companies with a board to refresh"})

        started = self.refresh_job.start(boards, bool(body.get("deep")))
        state = self.refresh_job.state()
        if not started:
            return ApiResponse(409, {"error": "a refresh is already running", "refresh": state})
        return ApiResponse(202, {"refresh": state})

    def process_state(self) -> ApiResponse:
        """Progress of the screen-and-read job, and what is waiting for one.

        `waiting.unscreened`: postings the funnel has not placed (added since
        the last screening). `waiting.unread`: postings that passed the rules
        and no model has read. Either being non-zero is the page's cue to
        offer the job.
        """
        return ApiResponse(
            200,
            {
                "process": self.process_job.state(),
                "can_read": self.process_job.can_read,
                "waiting": {
                    "unscreened": self.store.filtered_stage_counts().get(UNSCREENED, 0),
                    "unread": self.store.filtered_verdict_counts(stage=[str(Stage.MODEL)]).get(UNREAD, 0),
                },
            },
        )

    def process(self) -> ApiResponse:
        """Screen every posting, then have the model read what is unread.

        Runs in the background: 202 once started, 409 while one is running.
        Its progress comes back either way, and from GET /api/process.
        """
        started = self.process_job.start()
        response = self.process_state()
        if not started:
            return ApiResponse(409, {"error": "already screening and reading", **response.payload})
        return ApiResponse(202, response.payload)

    def health(self) -> ApiResponse:
        """Lets the extension popup show whether the server is reachable."""
        ruleset = getattr(self.classifier, "ruleset", None)
        return ApiResponse(
            200,
            {
                "ok": True,
                "classifier": self.classifier.name,
                "rules_version": getattr(ruleset, "version", None),
            },
        )

    def classify(self, body: Mapping[str, Any]) -> ApiResponse:
        """Classify bare text. Stores nothing.

        The posting is a throwaway wrapper because the Classifier protocol
        takes a Posting: a model classifier may want the title too.
        """
        text = body.get("text")
        if not isinstance(text, str):
            return ApiResponse(400, {"error": "text must be a string"})
        title = body.get("title")
        posting = Posting(
            platform=Platform.WEB,
            company_slug="",
            job_id="",
            title=title if isinstance(title, str) else "",
            location=None,
            url="",
            description_text=text,
        )
        return ApiResponse(
            200, {"decision": self.classifier.classify(posting).to_dict()}
        )

    def browsed(self, body: Mapping[str, Any]) -> ApiResponse:
        """Record a posting the extension saw, classified by the server."""
        from ..cli import classify_posting

        try:
            posting = posting_from_browsed(body)
        except BadPosting as exc:
            return ApiResponse(400, {"error": str(exc)})

        result = classify_posting(
            posting, posting.url, self.classifier, Origin.BROWSED
        )
        run_id = self.store.start_run("extension", 1)
        posting_id = self.store.record_result(run_id, result)
        self.store.finish_run(run_id)
        return ApiResponse(
            200,
            {
                "posting_id": posting_id,
                "decision": {
                    "label": str(result.label),
                    "confidence": result.confidence,
                    "evidence": result.evidence,
                    "matched_rule": result.matched_rule,
                    "matched_rules": list(result.matched_rules),
                    "classifier_name": result.classifier_name,
                },
            },
        )


def _filename(name: str) -> str:
    """An uploaded file's own name: no folders, no control characters.

    A long one is cut in its stem, so the extension survives.
    """
    name = PurePosixPath(_CONTROL_CHARS.sub("", name).replace("\\", "/")).name.strip()
    suffix = PurePosixPath(name).suffix
    return name[: len(name) - len(suffix)][: MAX_FILENAME_LENGTH - len(suffix)] + suffix


def _company_pairs(wanted: Any) -> list[tuple[str, str]]:
    """`[{platform, slug}, ...]` from a request body, or ValueError saying why not."""
    if not isinstance(wanted, list) or not wanted:
        raise ValueError("companies must be a non-empty list of {platform, slug}")
    pairs: list[tuple[str, str]] = []
    for entry in wanted:
        platform = entry.get("platform") if isinstance(entry, dict) else None
        slug = entry.get("slug") if isinstance(entry, dict) else None
        if not isinstance(platform, str) or not isinstance(slug, str):
            raise ValueError("each company needs a platform and a slug")
        pairs.append((platform, slug))
    return pairs


def _filters(query: Mapping[str, list[str]]) -> dict[str, Any]:
    """PostingFilter's fields from a query string: what /api/postings narrows by."""
    return {
        "label": _many(query, "label"),
        "company": _many(query, "company"),
        "exclude_companies": _many(query, "exclude"),
        "region": _many(query, "region"),
        "place": _many(query, "place"),
        "exclude_places": _many(query, "exclude_place"),
        "query": _one(query, "q"),
        "changed": _one(query, "changed") in ("1", "true"),
        "closing": _one(query, "closing") in ("1", "true"),
        "posted_within": _days(query, "posted"),
        "applied_on": _day(query, "applied_on"),
        "added_on": _day(query, "added_on"),
        "app_status": _many(query, "app"),
        "stage": _many(query, "stage"),
        "verdict": _many(query, "verdict"),
        "outcome": _many(query, "outcome"),
        "resume": _many(query, "resume"),
    }


# How /api/postings may sort: see Store.postings.
ORDERS = ("newest", "posted", "posted_asc", "deadline")


def _day(query: Mapping[str, list[str]], key: str) -> str | None:
    """A YYYY-MM-DD day, or None when absent or not one."""
    raw = _one(query, key)
    try:
        return date.fromisoformat(raw).isoformat() if raw and len(raw) == 10 else None
    except ValueError:
        return None


def _days(query: Mapping[str, list[str]], key: str) -> int | None:
    """A whole number of days, or None when absent or not one."""
    raw = _one(query, key)
    return int(raw) if raw is not None and raw.isdigit() else None


def _one(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def _many(query: Mapping[str, list[str]], key: str) -> list[str]:
    """Every non-blank value of a repeated parameter (?exclude=a&exclude=b)."""
    return [v.strip() for v in query.get(key) or [] if v.strip()]


def _int(
    query: Mapping[str, list[str]], key: str, default: int, maximum: int | None = None
) -> int:
    raw = _one(query, key)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    value = max(0, value)
    return min(value, maximum) if maximum is not None else value
