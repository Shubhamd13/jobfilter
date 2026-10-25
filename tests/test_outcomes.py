"""What came of an application: the steps it reached (OA, interview, offer, rejected).

Storage and the API on top of it. The page's buttons are exercised in
test_web_ui_outcomes.py.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from jobfilter.models import Outcome
from jobfilter.storage import SCHEMA_VERSION, NotApplied, Store
from jobfilter.web import Api

from .test_applications import seed

DAYS = [f"2026-10-0{day}T09:00:00+00:00" for day in range(1, 6)]


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Call it and every write from then on lands a day after the one before."""

    def start() -> None:
        days = iter(DAYS)
        monkeypatch.setattr("jobfilter.storage.store.utcnow", lambda: next(days))

    return start


def applied(store: Store) -> int:
    posting_id = seed(store)["swe"]
    store.set_application(posting_id, "applied")
    return posting_id


def steps(store: Store, posting_id: int) -> list[str]:
    """The stages an application reached, oldest first."""
    found = store.posting(posting_id)["outcome_steps"]
    return [step.split(":", 1)[0] for step in found.split(",")] if found else []


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def test_a_v7_database_keeps_its_marks_and_notes(tmp_path: Path) -> None:
    path = tmp_path / "v7.db"
    with Store(path) as store:
        ids = seed(store)
        store.set_application(ids["swe"], "applied", "sent through a referral")
        store.set_application(ids["mech"], "skipped")

    # Put it back to how version 7 left it: no resume on a mark, no new tables.
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        DROP VIEW posting_overview;
        DROP TABLE application_events;
        DROP TABLE resumes;
        ALTER TABLE applications DROP COLUMN resume_id;
        CREATE VIEW posting_overview AS SELECT id AS posting_id FROM postings;
        PRAGMA user_version = 7;
        """
    )
    legacy.close()

    with Store(path) as store:
        assert store.version == SCHEMA_VERSION
        swe = store.posting(ids["swe"])
        assert (swe["app_status"], swe["app_note"]) == ("applied", "sent through a referral")
        assert (swe["outcome"], swe["outcome_steps"], swe["resume_id"]) == (None, None, None)
        assert store.posting(ids["mech"])["app_status"] == "skipped"
        assert store.set_outcome(ids["swe"], "oa")
        assert store.posting(ids["swe"])["outcome"] == "oa"


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def test_an_application_starts_with_no_steps(store: Store) -> None:
    posting_id = applied(store)
    detail = store.posting(posting_id)
    assert (detail["outcome"], detail["outcome_steps"]) == (None, None)


def test_steps_are_kept_in_the_order_they_came(store: Store, clock: Callable[[], None]) -> None:
    posting_id = applied(store)
    clock()
    store.set_outcome(posting_id, "oa")
    store.set_outcome(posting_id, "interview")
    store.set_outcome(posting_id, "rejected")

    detail = store.posting(posting_id)
    assert detail["outcome_steps"] == f"oa:{DAYS[0]},interview:{DAYS[1]},rejected:{DAYS[2]}"
    assert detail["outcome"] == "rejected", "it still reached an interview: the steps say so"


def test_a_step_keeps_its_first_date(store: Store, clock: Callable[[], None]) -> None:
    posting_id = applied(store)
    clock()
    store.set_outcome(posting_id, "oa")
    store.set_outcome(posting_id, "oa")
    assert store.posting(posting_id)["outcome_steps"] == f"oa:{DAYS[0]}"


def test_a_step_can_be_taken_back(store: Store) -> None:
    posting_id = applied(store)
    store.set_outcome(posting_id, "oa")
    store.set_outcome(posting_id, "interview")

    assert store.set_outcome(posting_id, "interview", on=False)
    assert steps(store, posting_id) == ["oa"]
    assert store.posting(posting_id)["outcome"] == "oa"

    assert store.set_outcome(posting_id, "offer", on=False), "taking back a step never reached is fine"
    assert steps(store, posting_id) == ["oa"]


@pytest.mark.parametrize(
    ("reached", "expected"),
    [
        (["oa"], "oa"),
        (["interview", "oa"], "interview"),
        (["oa", "interview", "rejected"], "rejected"),
        (["rejected", "oa"], "rejected"),
        (["oa", "interview", "offer"], "offer"),
        (["rejected", "offer"], "offer"),
    ],
    ids=["oa", "furthest-step", "rejected-after-interview", "rejected-first", "offer", "offer-wins"],
)
def test_where_an_application_stands(store: Store, reached: list[str], expected: str) -> None:
    posting_id = applied(store)
    for stage in reached:
        store.set_outcome(posting_id, stage)
    assert store.posting(posting_id)["outcome"] == expected


def test_a_step_needs_an_applied_posting(store: Store) -> None:
    ids = seed(store)
    with pytest.raises(NotApplied):
        store.set_outcome(ids["swe"], "oa")

    store.set_application(ids["swe"], "maybe")
    with pytest.raises(NotApplied):
        store.set_outcome(ids["swe"], "oa")
    assert steps(store, ids["swe"]) == []


def test_an_unknown_stage_or_posting_is_reported(store: Store) -> None:
    posting_id = applied(store)
    with pytest.raises(ValueError):
        store.set_outcome(posting_id, "ghosted")
    assert store.set_outcome(999, "oa") is False


def test_clearing_the_mark_forgets_the_steps(store: Store) -> None:
    posting_id = applied(store)
    store.set_outcome(posting_id, "oa")

    store.set_application(posting_id, None)
    store.set_application(posting_id, "applied")
    assert steps(store, posting_id) == []


def test_steps_wait_out_a_change_of_mark(store: Store) -> None:
    """Applied -> Maybe hides the steps; back to Applied and they are there."""
    posting_id = applied(store)
    store.set_outcome(posting_id, "oa")

    store.set_application(posting_id, "maybe")
    assert store.posting(posting_id)["outcome"] is None
    assert store.posting(posting_id)["outcome_steps"] is None

    store.set_application(posting_id, "applied")
    assert steps(store, posting_id) == ["oa"]


def test_steps_survive_a_refetch(store: Store) -> None:
    ids = seed(store)
    store.set_application(ids["swe"], "applied")
    store.set_outcome(ids["swe"], "interview")

    assert seed(store)["swe"] == ids["swe"]
    assert steps(store, ids["swe"]) == ["interview"]


def test_the_table_rows_carry_the_steps(store: Store) -> None:
    posting_id = applied(store)
    store.set_outcome(posting_id, "oa")
    row = next(r for r in store.postings() if r["posting_id"] == posting_id)
    assert row["outcome"] == "oa" and row["outcome_steps"].startswith("oa:")


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@pytest.fixture
def api(store: Store) -> Api:
    return Api(store, lambda urls, expand=False: [])


def test_api_records_and_takes_back_a_step(api: Api, store: Store) -> None:
    posting_id = applied(store)
    path = f"/api/postings/{posting_id}/outcome"

    response = api.handle("POST", path, body={"stage": "oa"})
    assert (response.status, response.payload) == (
        200, {"posting_id": posting_id, "stage": "oa", "on": True}
    )
    api.handle("POST", path, body={"stage": "rejected", "on": True})
    detail = api.handle("GET", f"/api/postings/{posting_id}").payload["posting"]
    assert detail["outcome"] == "rejected" and steps(store, posting_id) == ["oa", "rejected"]

    assert api.handle("POST", path, body={"stage": "rejected", "on": False}).status == 200
    assert steps(store, posting_id) == ["oa"]


@pytest.mark.parametrize(
    "body",
    [{}, {"stage": "ghosted"}, {"stage": 3}, {"stage": "oa", "on": "yes"}],
    ids=["no-stage", "unknown-stage", "stage-not-a-string", "on-not-a-boolean"],
)
def test_api_refuses_a_bad_step(api: Api, store: Store, body: dict) -> None:
    posting_id = applied(store)
    assert api.handle("POST", f"/api/postings/{posting_id}/outcome", body=body).status == 400
    assert steps(store, posting_id) == []


def test_api_wants_the_posting_applied_first(api: Api, store: Store) -> None:
    posting_id = seed(store)["swe"]
    response = api.handle("POST", f"/api/postings/{posting_id}/outcome", body={"stage": "oa"})
    assert response.status == 409
    assert "Applied" in response.payload["error"]


def test_api_step_404s_for_an_unknown_posting(api: Api) -> None:
    assert api.handle("POST", "/api/postings/999/outcome", body={"stage": "oa"}).status == 404


def test_api_lists_the_steps_for_the_page(api: Api) -> None:
    listed = api.handle("GET", "/api/stats").payload["outcomes"]
    assert listed == ["oa", "interview", "offer", "rejected"] == [str(stage) for stage in Outcome]
