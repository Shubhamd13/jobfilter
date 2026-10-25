"""Command line entry point: parse -> fetch -> classify -> JSONL + tables."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Sequence, TextIO

from .classifiers import RuleClassifier, RuleSet
from .classifiers.base import Classifier
from .fetchers import FetcherRegistry, FetchError, HttpClient
from .models import Label, Origin, ParsedUrl, Platform, Posting, Result, Status
from .roles.reader import Reader
from .report import render_company_summary, render_refresh, render_results
from .storage import DB_ENV_VAR, DEFAULT_DB_FILENAME, Store, default_db_path
from .web.server import DEFAULT_HOST, DEFAULT_PORT
from .url_parser import parse_url

DEFAULT_WIDTH = 100
MIN_WIDTH = 60
MAX_WIDTH = 200
COMMENT_PREFIX = "#"


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 if every URL was classified, 1 if any failed, 2 on bad usage."""
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return serve(args)
    if args.command == "reclassify":
        return reclassify(args)
    if args.command == "refresh":
        return refresh(args)
    if args.command == "screen":
        return screen(args)
    if args.command == "read":
        return read(args)
    return check(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobfilter",
        description=(
            "Classify job postings by citizenship / export control / clearance "
            "restrictions."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check_cmd = sub.add_parser(
        "check",
        help="check job posting URLs",
        description=(
            "Each SOURCE is a text file of URLs (one per line, '#' comments and "
            "blank lines ignored) or a URL itself."
        ),
    )
    check_cmd.add_argument("sources", nargs="+", metavar="SOURCE")
    check_cmd.add_argument(
        "--out", type=Path, metavar="PATH", help="write results as JSONL to PATH"
    )
    check_cmd.add_argument(
        "--rules", type=Path, metavar="PATH", help="use an alternative rules.json"
    )
    check_cmd.add_argument(
        "--timeout", type=float, default=10.0, help="per-request timeout (default: 10)"
    )
    check_cmd.add_argument(
        "--width", type=int, default=None, help="table width (default: terminal width)"
    )
    check_cmd.add_argument(
        "--quiet", action="store_true", help="write JSONL only, print no tables"
    )
    check_cmd.add_argument(
        "--db",
        type=Path,
        metavar="PATH",
        help=f"SQLite database (default: ${DB_ENV_VAR} or ./{DEFAULT_DB_FILENAME})",
    )
    check_cmd.add_argument(
        "--no-db", action="store_true", help="do not record this run in the database"
    )
    check_cmd.add_argument(
        "--expand",
        action="store_true",
        help=(
            "also check every other job at each company seen "
            "(Ashby costs nothing extra; Greenhouse and Lever one request each; "
            "Workday one request per job, minutes for a large company)"
        ),
    )

    reclassify_cmd = sub.add_parser(
        "reclassify",
        help="re-run the rules over every stored posting",
        description=(
            "Classify every posting already in the database again with the current "
            "rules, without fetching anything. A posting gains a history entry only "
            "when its verdict changes. Run this after editing rules.json."
        ),
    )
    reclassify_cmd.add_argument(
        "--db",
        type=Path,
        metavar="PATH",
        help=f"SQLite database (default: ${DB_ENV_VAR} or ./{DEFAULT_DB_FILENAME})",
    )
    reclassify_cmd.add_argument(
        "--rules", type=Path, metavar="PATH", help="use an alternative rules.json"
    )
    reclassify_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )

    refresh_cmd = sub.add_parser(
        "refresh",
        help="re-read the boards of stored companies and report what changed",
        description=(
            "Fetch the board of every stored Ashby, Greenhouse, Lever, Workday, "
            "Rippling and Dover company again and report new, edited, relabeled, "
            "closed and reopened jobs. A job its board no longer lists is kept and "
            "marked closed. LinkedIn and Indeed postings have no board and are "
            "left alone."
        ),
    )
    refresh_cmd.add_argument(
        "--company",
        action="append",
        default=[],
        metavar="SLUG",
        help="refresh only this company (repeatable; default: every one)",
    )
    refresh_cmd.add_argument(
        "--deep",
        action="store_true",
        help=(
            "re-read every job on Workday, Rippling and Dover too, to catch "
            "edited descriptions (one request per job: minutes for a large "
            "company). Without it only jobs not yet stored are fetched there."
        ),
    )
    refresh_cmd.add_argument(
        "--db",
        type=Path,
        metavar="PATH",
        help=f"SQLite database (default: ${DB_ENV_VAR} or ./{DEFAULT_DB_FILENAME})",
    )
    refresh_cmd.add_argument(
        "--rules", type=Path, metavar="PATH", help="use an alternative rules.json"
    )
    refresh_cmd.add_argument(
        "--timeout", type=float, default=10.0, help="per-request timeout (default: 10)"
    )
    refresh_cmd.add_argument(
        "--width", type=int, default=None, help="table width (default: terminal width)"
    )

    screen_cmd = sub.add_parser(
        "screen",
        help="run the role funnel over every stored posting",
        description=(
            "Place every stored posting in the role funnel (location, restriction "
            "label, title rules, robotics focus) and store where each one stopped, "
            "for the web UI's filters. Fetches nothing. Run it after adding "
            "postings or editing role_rules.json / profile.json."
        ),
    )
    screen_cmd.add_argument(
        "--db",
        type=Path,
        metavar="PATH",
        help=f"SQLite database (default: ${DB_ENV_VAR} or ./{DEFAULT_DB_FILENAME})",
    )
    screen_cmd.add_argument(
        "--role-rules", type=Path, metavar="PATH", help="use an alternative role_rules.json"
    )
    screen_cmd.add_argument(
        "--profile", type=Path, metavar="PATH", help="use this profile (default: profile.json)"
    )

    read_cmd = sub.add_parser(
        "read",
        help="have a local model read the postings that passed the role rules",
        description=(
            "A model served by Ollama reads each posting the role rules left undecided "
            "and reports what the work is, the years it asks for and the degrees it "
            "accepts; the profile turns that into fit / stretch / skip. Only postings "
            "not read yet are read, each is stored as it lands, and nothing leaves "
            "this machine. Needs `ollama serve` running with the model pulled."
        ),
    )
    read_cmd.add_argument(
        "--db",
        type=Path,
        metavar="PATH",
        help=f"SQLite database (default: ${DB_ENV_VAR} or ./{DEFAULT_DB_FILENAME})",
    )
    read_cmd.add_argument(
        "--model", metavar="NAME", help="Ollama model (default: $JOBFILTER_OLLAMA_MODEL or qwen2.5:7b)"
    )
    read_cmd.add_argument(
        "--limit", type=int, metavar="N", help="read at most N postings this run"
    )
    read_cmd.add_argument(
        "--company",
        action="append",
        default=[],
        metavar="SLUG",
        help="read only this company's postings (repeatable)",
    )
    read_cmd.add_argument(
        "--redo", action="store_true", help="read again postings that already have a reading"
    )
    read_cmd.add_argument(
        "--role-rules", type=Path, metavar="PATH", help="use an alternative role_rules.json"
    )
    read_cmd.add_argument(
        "--profile", type=Path, metavar="PATH", help="use this profile (default: profile.json)"
    )

    serve_cmd = sub.add_parser(
        "serve",
        help="run the local web UI",
        description="Start a local web server for checking and browsing postings.",
    )
    serve_cmd.add_argument("--host", default=DEFAULT_HOST, help="bind address")
    serve_cmd.add_argument("--port", type=int, default=DEFAULT_PORT, help="port")
    serve_cmd.add_argument(
        "--db",
        type=Path,
        metavar="PATH",
        help=f"SQLite database (default: ${DB_ENV_VAR} or ./{DEFAULT_DB_FILENAME})",
    )
    serve_cmd.add_argument(
        "--timeout", type=float, default=10.0, help="per-request fetch timeout"
    )
    serve_cmd.add_argument(
        "--rules", type=Path, metavar="PATH", help="use an alternative rules.json"
    )
    serve_cmd.add_argument(
        "--open", action="store_true", help="open the UI in a browser on start"
    )
    return parser


def check(args: argparse.Namespace) -> int:
    try:
        urls = read_sources(args.sources)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not urls:
        print("error: no URLs found in the given sources", file=sys.stderr)
        return 2

    if not args.quiet:
        _show_progress()
    classifier = RuleClassifier(RuleSet.load(args.rules) if args.rules else None)
    with FetcherRegistry(HttpClient(timeout=args.timeout)) as registry:
        results = run(urls, registry, classifier)
        if args.expand:
            results += expand_companies(results, registry, classifier)

    if args.out:
        write_jsonl(results, args.out)

    db_path = None
    if not args.no_db:
        db_path = args.db or default_db_path()
        with Store(db_path) as store:
            store.record_run(source=" ".join(args.sources), results=results)

    if not args.quiet:
        report(results, sys.stdout, width=_width(args.width))
        if args.out:
            print(f"\nwrote {len(results)} result(s) to {args.out}")
        if db_path:
            print(f"recorded {len(results)} result(s) in {db_path}")

    return 1 if any(r.status is not Status.OK for r in results) else 0


def serve(args: argparse.Namespace) -> int:
    """Run the local web UI until interrupted."""
    from .web import Api, build_server, make_checker, make_refresher, make_store_reader

    db_path = args.db or default_db_path()
    classifier = RuleClassifier(RuleSet.load(args.rules) if args.rules else None)

    with Store(db_path) as store:
        api = Api(
            store,
            make_checker(classifier, timeout=args.timeout),
            classifier,
            make_refresher(store, classifier, timeout=args.timeout),
            reader=make_store_reader(),
        )
        server = build_server(api, host=args.host, port=args.port)
        port = server.server_address[1]
        url = f"http://{args.host}:{port}"

        print(f"jobfilter UI  {url}")
        print(f"database      {db_path}")
        print("Ctrl-C to stop")

        if args.open:
            import webbrowser

            webbrowser.open(url)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            server.server_close()
    return 0


def read_sources(sources: Sequence[str]) -> list[str]:
    """Expand each source: an existing file becomes its lines, else it is a URL.

    Duplicates are kept. Deciding two URLs are "the same" is the caller's
    business, and the Ashby board cache makes repeats nearly free anyway.
    """
    urls: list[str] = []
    for source in sources:
        path = Path(source)
        if path.is_file():
            urls.extend(read_url_file(path))
        else:
            urls.append(source.strip())
    return [u for u in urls if u]


def read_url_file(path: Path) -> list[str]:
    lines = path.read_text().splitlines()
    return [
        stripped
        for line in lines
        if (stripped := line.strip()) and not stripped.startswith(COMMENT_PREFIX)
    ]


def run(
    urls: Sequence[str], registry: FetcherRegistry, classifier: Classifier
) -> list[Result]:
    """One Result per job URL, in input order. Nothing is ever dropped.

    A board URL (Workday's `.../NVIDIAExternalCareerSite`) stands for every
    job on it, each a DISCOVERED result in the URL's place. A job that is also
    given by its own URL keeps that REQUESTED row and is not repeated.
    """
    per_url = [
        check_board(url, parsed, registry, classifier)
        if (parsed := parse_url(url)).is_board
        else [classify_url(url, registry, classifier)]
        for url in urls
    ]
    requested = {
        _key(r) for rows in per_url for r in rows
        if r.origin is Origin.REQUESTED and r.status is Status.OK
    }
    results: list[Result] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for rows in per_url:
        for result in rows:
            if result.origin is Origin.DISCOVERED and result.status is Status.OK:
                if _key(result) in requested or _key(result) in seen:
                    continue
                seen.add(_key(result))
            results.append(result)
    return results


def check_board(
    url: str, parsed: ParsedUrl, registry: FetcherRegistry, classifier: Classifier
) -> list[Result]:
    """Every job on a board URL. A board that cannot be read is one failed row."""
    try:
        items = registry.fetch_board_url(parsed)
    except FetchError as exc:
        return [
            _failed(url, Status.FETCH_ERROR, parsed.platform, type(exc).__name__,
                    str(exc), company_slug=parsed.slug)
        ]
    if not items:
        return [
            _failed(url, Status.FETCH_ERROR, parsed.platform, "EmptyBoard",
                    "the board lists no jobs for this search", company_slug=parsed.slug)
        ]
    return [board_item(item, parsed.platform, parsed.slug, classifier) for item in items]


def board_item(
    item: Posting | FetchError, platform: Platform, slug: str | None, classifier: Classifier
) -> Result:
    """A board entry as a DISCOVERED row: classified, or the error that stopped it."""
    if isinstance(item, FetchError):
        return _failed(
            item.url, Status.FETCH_ERROR, platform, type(item).__name__, str(item),
            company_slug=slug, job_id=parse_url(item.url).job_id, origin=Origin.DISCOVERED,
        )
    return classify_posting(item, item.url, classifier, Origin.DISCOVERED)


def _key(result: Result) -> tuple[str, str | None, str | None]:
    return (result.platform, result.company_slug, result.job_id)


def expand_companies(
    results: Sequence[Result], registry: FetcherRegistry, classifier: Classifier
) -> list[Result]:
    """Check every other job at each company already seen.

    Every platform here publishes a whole-board endpoint, and Ashby's is the
    one `fetch` already pulled to find a single job -- so expanding a company
    costs one request on Greenhouse and Lever and nothing at all on Ashby.
    Workday lists 20 jobs per request and needs one more per description.

    Jobs already covered by the requested URLs are skipped, so a result never
    appears twice.
    """
    seen = {
        (r.platform, r.company_slug, r.job_id)
        for r in results
        if r.status is Status.OK
    }
    companies = _companies_in(results, registry)

    discovered: list[Result] = []
    for platform, slug in companies:
        try:
            postings = registry.fetch_board(platform, slug)
        except FetchError as exc:
            discovered.append(
                _failed(
                    exc.url or f"{platform}:{slug}",
                    Status.FETCH_ERROR,
                    platform,
                    type(exc).__name__,
                    str(exc),
                    company_slug=slug,
                    origin=Origin.DISCOVERED,
                )
            )
            continue

        for item in postings:
            if isinstance(item, Posting):
                key = (item.platform, item.company_slug, item.job_id)
                if key in seen:
                    continue
                seen.add(key)
            discovered.append(board_item(item, platform, slug, classifier))

    return discovered


def _companies_in(
    results: Sequence[Result], registry: FetcherRegistry
) -> list[tuple[Platform, str]]:
    """Distinct (platform, slug) pairs worth expanding, in first-seen order."""
    companies: list[tuple[Platform, str]] = []
    for result in results:
        if result.company_slug is None or not registry.supports_boards(result.platform):
            continue
        pair = (result.platform, result.company_slug)
        if pair not in companies:
            companies.append(pair)
    return companies


def classify_url(
    url: str, registry: FetcherRegistry, classifier: Classifier
) -> Result:
    parsed = parse_url(url)

    if not parsed.supported:
        return _failed(
            url,
            Status.UNSUPPORTED,
            parsed.platform,
            "unsupported",
            "no fetcher for this URL shape",
        )

    try:
        posting = registry.fetch(parsed)
    except FetchError as exc:
        return _failed(
            url,
            Status.FETCH_ERROR,
            parsed.platform,
            type(exc).__name__,
            str(exc),
            company_slug=parsed.slug,
            job_id=parsed.job_id,
        )
    except Exception as exc:  # noqa: BLE001 - one bad URL must not end the run
        return _failed(
            url,
            Status.FETCH_ERROR,
            parsed.platform,
            type(exc).__name__,
            str(exc),
            company_slug=parsed.slug,
            job_id=parsed.job_id,
        )

    return classify_posting(posting, url, classifier, Origin.REQUESTED)


def reclassify(args: argparse.Namespace) -> int:
    """Run the current rules over stored postings; print what changed."""
    classifier = RuleClassifier(RuleSet.load(args.rules) if args.rules else None)
    with Store(args.db or default_db_path()) as store:
        changes = reclassify_store(store, classifier, dry_run=args.dry_run)
        total = len(store.stored_postings(classifier.name))
    print(render_label_changes(changes, total, dry_run=args.dry_run))
    return 0


def screen(args: argparse.Namespace) -> int:
    """Run the role funnel over stored postings; print postings per stage."""
    from .roles import Profile, RoleRules, screen_store

    rules = RoleRules.load(args.role_rules)
    profile = Profile.load(args.profile)
    with Store(args.db or default_db_path()) as store:
        stages = screen_store(store, rules, profile)
    print(f"{sum(stages.values()):,} postings screened.")
    for stage, n in stages.items():
        print(f"  {n:6,}  {stage}")
    return 0


def read(args: argparse.Namespace, reader: "Reader | None" = None) -> int:
    """Have a local model read what passed the rules; exit 1 if it could not."""
    from .roles import OllamaReader, Profile, RoleRules, read_store

    rules = RoleRules.load(args.role_rules)
    profile = Profile.load(args.profile)
    if profile.fit is None:
        print("error: the profile has no \"fit\" section, so no verdict can be given", file=sys.stderr)
        return 2
    reader = reader or OllamaReader(model=args.model, degree=profile.fit.highest_degree)

    def progress(done: int, total: int, seconds: float) -> None:
        if done % 10 == 0 or done == total:
            left = (total - done) * seconds / done
            print(f"  {done}/{total} read, {seconds / done:.1f}s each, about {left / 60:.0f} min left", flush=True)

    with Store(args.db or default_db_path()) as store:
        summary = read_store(
            store, reader, rules=rules, profile=profile, limit=args.limit,
            companies=args.company, redo=args.redo, progress=progress,
        )
        verdicts = store.filtered_verdict_counts(stage=["model"])

    print(f"{summary.read} postings read by {reader.name} in {summary.seconds / 60:.1f} min"
          + (f" ({summary.per_posting:.1f}s each)." if summary.read else "."))
    if summary.failed:
        print(f"{summary.failed} could not be read and will be tried again next time.")
    if summary.stopped:
        print(f"Stopped early: {summary.stopped}", file=sys.stderr)
    if summary.waiting:
        print(f"{summary.waiting} postings that passed the rules are still unread.")
    print("Of the postings that passed the rules:")
    for verdict in ("best", "fit", "stretch", "skip", "unknown", "unread"):
        print(f"  {verdicts.get(verdict, 0):6,}  {verdict}")
    return 1 if summary.stopped else 0


def refresh(args: argparse.Namespace) -> int:
    """Re-read stored companies' boards; exit 1 if any board or job failed."""
    from .refresh import refresh_boards, stored_boards

    _show_progress()
    classifier = RuleClassifier(RuleSet.load(args.rules) if args.rules else None)
    with Store(args.db or default_db_path()) as store:
        with FetcherRegistry(HttpClient(timeout=args.timeout)) as registry:
            boards = stored_boards(store, registry)
            if args.company:
                unknown = sorted(set(args.company) - {board.slug for board in boards})
                if unknown:
                    print(
                        f"error: no stored company with a board: {', '.join(unknown)}",
                        file=sys.stderr,
                    )
                    return 2
                boards = [board for board in boards if board.slug in args.company]
            outcomes = refresh_boards(store, registry, classifier, boards, deep=args.deep)

    print(render_refresh(outcomes, _width(args.width)))
    return 1 if any(not o.ok or o.failed for o in outcomes) else 0


def reclassify_store(
    store: Store, classifier: Classifier, dry_run: bool = False
) -> dict[tuple[str | None, str], int]:
    """(old label, new label) -> how many postings moved that way."""
    changes: dict[tuple[str | None, str], int] = {}
    for posting_id, posting, old in store.stored_postings(classifier.name):
        result = classify_posting(posting, posting.url, classifier, Origin.REQUESTED)
        if str(result.label) == old:
            # Same verdict; record_classification still notes a changed
            # quote or rule list, which is how history stays honest.
            if not dry_run:
                store.record_classification(posting_id, result)
            continue
        key = (old, str(result.label))
        changes[key] = changes.get(key, 0) + 1
        if not dry_run:
            store.record_classification(posting_id, result)
    return changes


def render_label_changes(
    changes: dict[tuple[str | None, str], int], total: int, dry_run: bool = False
) -> str:
    moved = sum(changes.values())
    verb = "would change" if dry_run else "changed"
    lines = [f"{total} postings reclassified; {moved} {verb} label."]
    for (old, new), n in sorted(changes.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {n:5}  {old or '(none)':22} -> {new}")
    return "\n".join(lines)


def classify_posting(
    posting: Posting, url: str, classifier: Classifier, origin: Origin
) -> Result:
    decision = classifier.classify(posting)
    return Result(
        url=url,
        status=Status.OK,
        platform=posting.platform,
        company_slug=posting.company_slug,
        job_id=posting.job_id,
        title=posting.title,
        location=posting.location,
        label=decision.label,
        confidence=decision.confidence,
        evidence=decision.evidence,
        matched_rule=decision.matched_rule,
        matched_rules=decision.matched_rules,
        classifier_name=decision.classifier_name,
        error=None,
        error_type=None,
        origin=origin,
        posting=posting,
    )


def write_jsonl(results: Sequence[Result], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")


def report(results: list[Result], stream: TextIO, width: int = DEFAULT_WIDTH) -> None:
    print(render_results(results, width), file=stream)
    print(file=stream)
    print(render_company_summary(results, width), file=stream)


def _failed(
    url: str,
    status: Status,
    platform: Platform,
    error_type: str,
    error: str,
    *,
    company_slug: str | None = None,
    job_id: str | None = None,
    origin: Origin = Origin.REQUESTED,
) -> Result:
    """A URL we could not classify still gets a row, labelled UNCLEAR."""
    return Result(
        url=url,
        status=status,
        platform=platform,
        company_slug=company_slug,
        job_id=job_id,
        title=None,
        location=None,
        label=Label.UNCLEAR,
        confidence=None,
        evidence=None,
        matched_rule=None,
        matched_rules=(),
        classifier_name=None,
        error=error,
        error_type=error_type,
        origin=origin,
    )


def _show_progress() -> None:
    """Long fetches (a Workday board) report progress on stderr.

    Only jobfilter's own loggers: httpx logs every request at INFO.
    """
    logger = logging.getLogger("jobfilter")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _width(requested: int | None) -> int:
    width = requested or shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
    return max(MIN_WIDTH, min(MAX_WIDTH, width))
