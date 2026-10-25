"""Screening and reading new postings as one background job the page can poll.

Placing every posting takes a few seconds and a model reads about one posting
every two, so a batch of new jobs is too slow for one request. `start` returns
at once; `state` is what `GET /api/process` serves while the page waits.

The job screens first, so the page's filters are right within seconds, then has
the model read whatever passed the rules and has no reading yet.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ..roles import ReadSummary
from ..storage import Store
from ..storage.store import utcnow

# Runs the role funnel over the store; returns postings per stage.
Screener = Callable[[Store], dict[str, int]]
# (done, total, seconds so far), as roles.screening.Progress.
Progress = Callable[[int, int, float], None]
# Has a model read what passed the rules, reporting through `progress`.
StoreReader = Callable[[Store, Progress], ReadSummary]


def idle_state() -> dict[str, Any]:
    """The state before any job has run."""
    return {
        "running": False,
        "phase": None,  # "screening", then "reading"
        "done": 0,
        "total": 0,
        "seconds_each": None,
        "started_at": None,
        "finished_at": None,
        "stages": None,
        "read": 0,
        "failed": 0,
        # Why the reading stopped early (the model is down), if it did.
        "stopped": None,
        "error": None,
    }


class ProcessJob:
    """At most one screen-and-read at a time, on a background thread."""

    def __init__(self, store: Store, screener: Screener, reader: StoreReader | None) -> None:
        self._store = store
        self._screener = screener
        # None: this server cannot read (tests, or no model set up); it screens only.
        self._reader = reader
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = idle_state()

    @property
    def can_read(self) -> bool:
        return self._reader is not None

    def start(self) -> bool:
        """Begin screening, then reading. False if a job is already running."""
        with self._lock:
            if self._state["running"]:
                return False
            self._state = {
                **idle_state(), "running": True, "phase": "screening", "started_at": utcnow(),
            }
            self._thread = threading.Thread(
                target=self._run, name="jobfilter-process", daemon=True
            )
            self._thread.start()
        return True

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def join(self, timeout: float | None = None) -> None:
        """Wait for the running job, if any. For tests and shutdown."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        error: str | None = None
        try:
            stages = self._screener(self._store)
            self._update(stages=stages)
            if self._reader is not None:
                self._update(phase="reading")
                summary = self._reader(self._store, self._progress)
                self._update(
                    read=summary.read,
                    failed=summary.failed,
                    stopped=summary.stopped,
                    seconds_each=round(summary.per_posting, 1) if summary.read else None,
                )
        except Exception as exc:  # noqa: BLE001 - the page must learn the job died
            error = f"{type(exc).__name__}: {exc}"
        self._update(running=False, phase=None, finished_at=utcnow(), error=error)

    def _progress(self, done: int, total: int, seconds: float) -> None:
        self._update(done=done, total=total, seconds_each=round(seconds / done, 1) if done else None)

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)
