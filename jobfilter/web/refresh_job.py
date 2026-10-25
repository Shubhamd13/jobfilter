"""A board refresh as a background job the page can poll.

Refreshing every stored company takes over a minute, and a deep refresh of a
large Workday board many more -- too long for one request. `start` returns at
once; `state` is what `GET /api/refresh` serves while the page waits.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Sequence

from ..models import CompanyRefresh
from ..storage import BoardCompany
from ..storage.store import utcnow

# Same shape as jobfilter.refresh.Progress; spelled out here because importing
# that module from the web package would be circular (it imports the CLI).
Progress = Callable[[Sequence[CompanyRefresh], int, BoardCompany | None], None]
# (boards, deep, progress) -> one outcome per board.
Refresher = Callable[[Sequence[BoardCompany], bool, Progress], list[CompanyRefresh]]


def idle_state() -> dict[str, Any]:
    """The state before any refresh has run."""
    return {
        "running": False,
        "deep": False,
        "total": 0,
        "done": 0,
        "current": None,
        "started_at": None,
        "finished_at": None,
        "results": [],
        "error": None,
    }


class RefreshJob:
    """At most one refresh at a time, on a background thread."""

    def __init__(self, refresher: Refresher) -> None:
        self._refresher = refresher
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = idle_state()

    def start(self, boards: Sequence[BoardCompany], deep: bool = False) -> bool:
        """Begin refreshing `boards`. False if a refresh is already running."""
        with self._lock:
            if self._state["running"]:
                return False
            self._state = {
                **idle_state(),
                "running": True,
                "deep": deep,
                "total": len(boards),
                "started_at": utcnow(),
            }
            self._thread = threading.Thread(
                target=self._run, args=(list(boards), deep),
                name="jobfilter-refresh", daemon=True,
            )
            self._thread.start()
        return True

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def join(self, timeout: float | None = None) -> None:
        """Wait for the running refresh, if any. For tests and shutdown."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, boards: list[BoardCompany], deep: bool) -> None:
        error: str | None = None
        try:
            self._refresher(boards, deep, self._progress)
        except Exception as exc:  # noqa: BLE001 - the page must learn the refresh died
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._state.update(
                running=False, current=None, finished_at=utcnow(), error=error
            )

    def _progress(
        self, outcomes: Sequence[CompanyRefresh], total: int, current: BoardCompany | None
    ) -> None:
        with self._lock:
            self._state.update(
                done=len(outcomes),
                total=total,
                results=[outcome.to_dict() for outcome in outcomes],
                current=None if current is None else {
                    "platform": str(current.platform),
                    "slug": current.slug,
                    "name": current.name,
                },
            )
