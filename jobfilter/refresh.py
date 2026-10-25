"""Re-read the boards of companies already stored, and say what changed.

A refresh starts from the database, not from URLs: every stored company on a
platform that can list a board is read again and compared with what is held.

    new        the board lists a job that is not stored
    edited     a stored job's description changed
    relabeled  its verdict changed
    closed     the board no longer lists a stored job (kept, marked closed)
    reopened   a closed job is listed again

Two ways to read a board. Ashby, Greenhouse and Lever hand over every job
with its text in one request, so they are always read whole. Workday, Rippling
and Dover cost one request per job, so a quick refresh lists the board and
fetches only the jobs it does not hold -- which finds new and closed jobs but
not edits. `deep=True` reads every job on those too.

LinkedIn and Indeed postings are never refreshed: there is no board to read.
"""

from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Sequence

from .classifiers.base import Classifier
from .cli import board_item, classify_posting
from .fetchers import FetcherRegistry, FetchError
from .models import Change, CompanyRefresh, Origin, ParsedUrl, Posting, Result
from .storage import BoardCompany, Store, StoredJob, content_hash
from .url_parser import parse_url

log = logging.getLogger(__name__)

WORKERS = 4  # parallel fetches of jobs new to a listed board

# Called before each board (`current` set) and once at the end (`current` None).
Progress = Callable[[Sequence[CompanyRefresh], int, BoardCompany | None], None]


def stored_boards(store: Store, registry: FetcherRegistry) -> list[BoardCompany]:
    """Every stored company whose platform has a board to re-read."""
    return store.board_companies(registry.board_platforms())


def refresh_boards(
    store: Store,
    registry: FetcherRegistry,
    classifier: Classifier,
    boards: Sequence[BoardCompany],
    *,
    deep: bool = False,
    progress: Progress | None = None,
) -> list[CompanyRefresh]:
    """Refresh each board in turn, as one recorded run. One outcome per board."""
    run_id = store.start_run("refresh+deep" if deep else "refresh", len(boards))
    outcomes: list[CompanyRefresh] = []
    for index, board in enumerate(boards, start=1):
        if progress:
            progress(outcomes, len(boards), board)
        log.info("refresh: %d/%d %s (%s)", index, len(boards), board.slug, board.platform)
        outcomes.append(refresh_company(store, registry, classifier, board, run_id, deep))
    store.finish_run(run_id)
    if progress:
        progress(outcomes, len(boards), None)
    return outcomes


def refresh_company(
    store: Store,
    registry: FetcherRegistry,
    classifier: Classifier,
    board: BoardCompany,
    run_id: int,
    deep: bool = False,
) -> CompanyRefresh:
    """Re-read one board and record what differs from the stored postings."""
    known = store.company_jobs(board.company_id, classifier.name)
    for job in known.values():
        registry.learn(parse_url(job.url))

    try:
        listed, items = _read_board(registry, board, known, deep)
    except FetchError as exc:
        # The board could not be read at all: that says nothing about its
        # jobs, so none is closed. The failure is a row, like any other.
        exc.url = exc.url or f"{board.platform}:{board.slug}"
        store.record_result(run_id, board_item(exc, board.platform, board.slug, classifier))
        store.record_board_refresh(run_id, board.company_id, "error", error=str(exc))
        return CompanyRefresh(
            board.platform, board.slug, board.name, "error", error=str(exc)
        )

    changes: Counter[Change] = Counter()
    failed = 0
    recorded: set[str] = set()
    for item in items:
        if isinstance(item, FetchError):
            store.record_result(run_id, board_item(item, board.platform, board.slug, classifier))
            failed += 1
            continue
        if item.job_id in recorded:
            continue  # one job on two of a tenant's Workday sites
        recorded.add(item.job_id)
        result = classify_posting(item, item.url, classifier, Origin.DISCOVERED)
        found = _changes(known.get(item.job_id), result)
        posting_id = store.record_result(run_id, result)
        for kind, detail in found:
            store.record_change(run_id, posting_id, kind, detail)
            changes[kind] += 1

    gone = [
        job.posting_id
        for job_id, job in known.items()
        if job.closed_at is None and job_id not in listed
    ]
    store.close_postings(run_id, gone)
    if gone:
        changes[Change.CLOSED] = len(gone)

    store.record_board_refresh(run_id, board.company_id, "ok", listed=len(listed))
    return CompanyRefresh(
        board.platform, board.slug, board.name, "ok",
        listed=len(listed), changes=dict(changes), failed=failed,
    )


def _read_board(
    registry: FetcherRegistry,
    board: BoardCompany,
    known: dict[str, StoredJob],
    deep: bool,
) -> tuple[set[str], list[Posting | FetchError]]:
    """The job ids the board lists now, and every job that was fetched."""
    if not deep and registry.lists_boards(board.platform):
        refs = registry.list_board(board.platform, board.slug)
        unseen = [ref for ref in refs if _unseen(known.get(ref.job_id or ""))]
        return {ref.job_id for ref in refs if ref.job_id}, _fetch_all(registry, unseen)

    items = list(registry.fetch_board(board.platform, board.slug))
    listed = {_job_id(item) for item in items}
    return {job_id for job_id in listed if job_id}, items


def _unseen(job: StoredJob | None) -> bool:
    """Not stored, or stored as closed: either way its text must be read."""
    return job is None or job.closed_at is not None


def _job_id(item: Posting | FetchError) -> str | None:
    """A job the board lists but that would not fetch is still listed."""
    return parse_url(item.url).job_id if isinstance(item, FetchError) else item.job_id


def _fetch_all(
    registry: FetcherRegistry, refs: Sequence[ParsedUrl]
) -> list[Posting | FetchError]:
    """Fetch each listed job. One that fails comes back as its error, not dropped."""

    def one(ref: ParsedUrl) -> Posting | FetchError:
        try:
            return registry.fetch(ref)
        except FetchError as exc:
            exc.url = ref.url  # the job's page, not the API call behind it
            return exc
        except Exception as exc:  # noqa: BLE001 - one bad job must not end the refresh
            return FetchError(
                f"{type(exc).__name__}: {exc}", url=ref.url, platform=ref.platform
            )

    if not refs:
        return []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(one, refs))


def _changes(old: StoredJob | None, result: Result) -> list[tuple[Change, str | None]]:
    """What differs between a stored job and the same job as just fetched."""
    if old is None:
        return [(Change.NEW, None)]

    found: list[tuple[Change, str | None]] = []
    if old.closed_at is not None:
        found.append((Change.REOPENED, None))
    if result.posting and content_hash(result.posting.description_text) != old.content_hash:
        found.append((Change.EDITED, None))
    if old.label is not None and old.label != str(result.label):
        found.append((Change.RELABELED, f"{old.label} -> {result.label}"))
    return found
