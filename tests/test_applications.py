"""Your marks (applied / skipped / maybe) and the stored role screening.

Storage, the API on top of it, and the `screen` command. The page's buttons are
exercised in test_web_ui_marks.py.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from jobfilter.cli import main
from jobfilter.models import AppStatus, Label
from jobfilter.roles import Profile, RoleRules, Stage, screen_store
from jobfilter.storage import NO_STATUS, SCHEMA_VERSION, UNSCREENED, Store
from jobfilter.web import Api

from .test_roles import PROFILE, ROBOTICS_JD
from .test_storage import make_posting, make_result

# job id -> (title, location, label): one posting per funnel stage.
JOBS = {
    "swe": ("Robotics Software Engineer", "San Francisco, CA", Label.OPEN),
    "senior": ("Senior Autonomy Engineer", "Remote", Label.UNCLEAR),
    "recruiter": ("Technical Recruiter", "Austin, TX", Label.OPEN),
    "staff": ("Staff Robotics Engineer", "Austin, TX", Label.OPEN),
    "mech": ("Mechanical Engineer, Robotics", "Austin, TX", Label.OPEN),
    "cleared": ("Robotics Software Engineer", "Austin, TX", Label.RESTRICTED_CLEARANCE),
    "london": ("Robotics Software Engineer", "London", Label.OPEN),
}
STAGES = {
    "swe": Stage.MODEL,
    "senior": Stage.MODEL,
    "recruiter": Stage.NON_ENGINEERING,
    "staff": Stage.SENIORITY,
    "mech": Stage.MECH_ELEC,
    "cleared": Stage.RESTRICTED,
    "london": Stage.LOCATION,
}


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as opened:
        yield opened


def seed(store: Store) -> dict[str, int]:
    """Record one posting per JOBS entry; job id -> posting id."""
    results = [
        make_result(
            dataclasses.replace(
                make_posting(job_id=job_id, title=title, text=ROBOTICS_JD), location=location
            ),
            label=label,
        )
        for job_id, (title, location, label) in JOBS.items()
    ]
    store.record_run("t", results)
    return {row["job_id"]: row["posting_id"] for row in store.postings()}


def screened(store: Store) -> dict[str, int]:
    ids = seed(store)
    screen_store(store, RoleRules.load(), Profile.from_dict(PROFILE))
    return ids


def overview(store: Store, posting_id: int) -> dict:
    return store.posting(posting_id)


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def test_a_v4_database_gains_marks_and_screens_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    with Store(path) as store:
        seed(store)

    # Put it back to how version 4 left it: no new tables, the old view.
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        DROP TABLE applications;
        DROP TABLE role_screens;
        DROP VIEW posting_overview;
        CREATE VIEW posting_overview AS SELECT id AS posting_id FROM postings;
        PRAGMA user_version = 4;
        """
    )
    legacy.close()

    with Store(path) as store:
        assert store.version == SCHEMA_VERSION
        rows = store.postings()
        assert len(rows) == len(JOBS)
        assert rows[0]["app_status"] is None and rows[0]["screen_stage"] is None
        assert store.set_application(rows[0]["posting_id"], "applied")


# --------------------------------------------------------------------------
# Marks
# --------------------------------------------------------------------------

def test_mark_change_and_clear(store: Store) -> None:
    posting_id = seed(store)["swe"]
    assert overview(store, posting_id)["app_status"] is None

    assert store.set_application(posting_id, "applied")
    marked = overview(store, posting_id)
    assert marked["app_status"] == "applied" and marked["app_updated_at"]

    store.set_application(posting_id, AppStatus.SKIPPED)
    assert overview(store, posting_id)["app_status"] == "skipped"

    store.set_application(posting_id, None)
    assert overview(store, posting_id)["app_status"] is None


def test_a_remark_keeps_the_note_unless_one_is_given(store: Store) -> None:
    posting_id = seed(store)["swe"]
    store.set_application(posting_id, "maybe", "ask about relocation")

    store.set_application(posting_id, "applied")
    assert overview(store, posting_id)["app_note"] == "ask about relocation"

    store.set_application(posting_id, "applied", "  referred by Sam  ")
    assert overview(store, posting_id)["app_note"] == "referred by Sam"

    store.set_application(posting_id, "applied", "")
    assert overview(store, posting_id)["app_note"] is None


def test_a_note_does_not_move_when_it_was_marked(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    posting_id = seed(store)["swe"]
    clock = iter(["2026-10-01T09:00:00+00:00", "2026-10-02T09:00:00+00:00", "2026-10-03T09:00:00+00:00"])
    monkeypatch.setattr("jobfilter.storage.store.utcnow", lambda: next(clock))

    store.set_application(posting_id, "applied")
    store.set_application(posting_id, "applied", "sent through a referral")
    assert overview(store, posting_id)["app_updated_at"] == "2026-10-01T09:00:00+00:00"

    store.set_application(posting_id, "maybe")
    assert overview(store, posting_id)["app_updated_at"] == "2026-10-03T09:00:00+00:00"


def test_marking_an_unknown_posting_reports_it(store: Store) -> None:
    assert store.set_application(999, "applied") is False


def test_an_unknown_status_is_refused(store: Store) -> None:
    posting_id = seed(store)["swe"]
    with pytest.raises(ValueError):
        store.set_application(posting_id, "liked")
    assert overview(store, posting_id)["app_status"] is None


def test_a_mark_survives_a_refetch(store: Store) -> None:
    posting_id = seed(store)["swe"]
    store.set_application(posting_id, "applied", "sent 3 Oct")

    title, location, label = JOBS["swe"]
    edited = dataclasses.replace(
        make_posting(job_id="swe", title=title, text="A rewritten description."), location=location
    )
    store.record_run("t", [make_result(edited, label=label)])

    again = overview(store, posting_id)
    assert (again["app_status"], again["app_note"]) == ("applied", "sent 3 Oct")
    assert again["description_text"] == "A rewritten description."


@pytest.mark.parametrize(
    "wanted,expected",
    [
        (["applied"], {"swe"}),
        (["applied", "maybe"], {"swe", "senior"}),
        ([NO_STATUS], set(JOBS) - {"swe", "senior", "mech"}),
        (["skipped", NO_STATUS], set(JOBS) - {"swe", "senior"}),
        (None, set(JOBS)),
    ],
)
def test_filter_by_mark(store: Store, wanted: list[str] | None, expected: set[str]) -> None:
    ids = seed(store)
    store.set_application(ids["swe"], "applied")
    store.set_application(ids["senior"], "maybe")
    store.set_application(ids["mech"], "skipped")

    found = {row["job_id"] for row in store.postings(app_status=wanted)}
    assert found == expected
    assert store.count_postings(app_status=wanted) == len(expected)


def test_mark_counts_follow_every_filter_but_their_own(store: Store) -> None:
    ids = seed(store)
    store.set_application(ids["swe"], "applied")
    store.set_application(ids["london"], "skipped")

    everywhere = store.filtered_app_counts(app_status=["applied"])
    assert everywhere == {"none": 5, "applied": 1, "skipped": 1, "maybe": 0}

    in_the_us = store.filtered_app_counts(region=["us"])
    assert in_the_us["skipped"] == 0 and in_the_us["applied"] == 1


# --------------------------------------------------------------------------
# The stored screening
# --------------------------------------------------------------------------

def test_a_new_posting_is_unscreened(store: Store) -> None:
    seed(store)
    assert store.filtered_stage_counts() == {UNSCREENED: len(JOBS)}
    assert store.count_postings(stage=[UNSCREENED]) == len(JOBS)
    assert store.count_postings(stage=["model"]) == 0


def test_screen_store_places_every_posting(store: Store) -> None:
    ids = seed(store)
    counts = screen_store(store, RoleRules.load(), Profile.from_dict(PROFILE))

    assert set(counts) == {str(stage) for stage in Stage}, "every stage is listed, even at zero"
    assert counts["model"] == 2 and counts["off_focus"] == 0
    assert sum(counts.values()) == len(JOBS)
    for job_id, stage in STAGES.items():
        assert overview(store, ids[job_id])["screen_stage"] == stage, job_id


def test_a_screening_stores_why(store: Store) -> None:
    ids = screened(store)

    cut = overview(store, ids["mech"])
    assert (cut["screen_reason"], cut["screen_evidence"]) == ("Mechanical", "Mechanical")

    kept = overview(store, ids["swe"])
    assert (kept["screen_focus_by"], kept["screen_focus_evidence"]) == ("title", "Robotics")


def test_screening_again_replaces_the_last_one(store: Store) -> None:
    ids = screened(store)
    keeps_staff = Profile.from_dict(
        {**PROFILE, "skip_categories": [c for c in PROFILE["skip_categories"] if c != "staff_plus"]}
    )
    counts = screen_store(store, RoleRules.load(), keeps_staff)

    assert counts["model"] == 3 and counts["seniority"] == 0
    assert overview(store, ids["staff"])["screen_stage"] == "model"
    assert sum(store.filtered_stage_counts().values()) == len(JOBS), "no row is stored twice"


def test_filter_by_stage(store: Store) -> None:
    screened(store)
    late = make_posting(job_id="late", title="Robotics Software Engineer", text=ROBOTICS_JD)
    store.record_run("t", [make_result(late)])

    assert {r["job_id"] for r in store.postings(stage=["model"])} == {"swe", "senior"}
    assert {r["job_id"] for r in store.postings(stage=["mech_elec", UNSCREENED])} == {"mech", "late"}
    assert store.filtered_stage_counts(stage=["model"])[UNSCREENED] == 1


def test_for_me_is_what_passed_and_is_not_marked(store: Store) -> None:
    ids = screened(store)
    store.set_application(ids["swe"], "applied")

    for_me = store.postings(stage=["model"], app_status=[NO_STATUS])
    assert [row["job_id"] for row in for_me] == ["senior"]


def test_a_mark_outlives_a_rescreen(store: Store) -> None:
    ids = screened(store)
    store.set_application(ids["swe"], "applied")
    screen_store(store, RoleRules.load(), Profile.from_dict(PROFILE))
    assert overview(store, ids["swe"])["app_status"] == "applied"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@pytest.fixture
def api(store: Store) -> Api:
    return Api(
        store,
        lambda urls, expand=False: [],
        screener=lambda s: screen_store(s, RoleRules.load(), Profile.from_dict(PROFILE)),
    )


def test_api_marks_a_posting(api: Api, store: Store) -> None:
    posting_id = seed(store)["swe"]
    path = f"/api/postings/{posting_id}/application"

    response = api.handle("POST", path, body={"status": "maybe", "note": "check the visa line"})
    assert (response.status, response.payload) == (200, {"posting_id": posting_id, "status": "maybe"})

    api.handle("POST", path, body={"status": "applied"})
    detail = api.handle("GET", f"/api/postings/{posting_id}").payload["posting"]
    assert (detail["app_status"], detail["app_note"]) == ("applied", "check the visa line")

    assert api.handle("POST", path, body={"status": None}).status == 200
    assert api.handle("GET", f"/api/postings/{posting_id}").payload["posting"]["app_status"] is None


@pytest.mark.parametrize(
    "body",
    [{"status": "liked"}, {"status": 1}, {"status": "applied", "note": 5}],
    ids=["unknown-status", "status-not-a-string", "note-not-a-string"],
)
def test_api_refuses_a_bad_mark(api: Api, store: Store, body: dict) -> None:
    posting_id = seed(store)["swe"]
    response = api.handle("POST", f"/api/postings/{posting_id}/application", body=body)
    assert response.status == 400
    assert api.handle("GET", f"/api/postings/{posting_id}").payload["posting"]["app_status"] is None


def test_api_mark_404s_for_an_unknown_posting(api: Api) -> None:
    assert api.handle("POST", "/api/postings/999/application", body={"status": "applied"}).status == 404


def test_api_screens_and_filters(api: Api, store: Store) -> None:
    ids = seed(store)

    response = api.handle("POST", "/api/screen")
    assert response.status == 200
    assert response.payload["screened"] == len(JOBS) and response.payload["stages"]["model"] == 2

    api.handle("POST", f"/api/postings/{ids['swe']}/application", body={"status": "skipped"})
    page = api.handle("GET", "/api/postings", {"stage": ["model"], "app": ["none"]}).payload

    assert [p["job_id"] for p in page["postings"]] == ["senior"]
    assert page["app_counts"] == {"none": 1, "applied": 0, "skipped": 1, "maybe": 0}
    assert page["stage_counts"]["model"] == 1 and page["stage_counts"]["location"] == 1
    json.dumps(page)  # everything in the payload is plain JSON


def test_api_lists_statuses_and_stages_for_the_page(api: Api) -> None:
    stats = api.handle("GET", "/api/stats").payload
    assert stats["app_statuses"] == ["none", "maybe", "applied", "skipped"]
    assert stats["stages"][0] == "model" and stats["stages"][-1] == "unscreened"
    assert set(stats["stages"]) == {str(s) for s in Stage} | {"unscreened"}
    json.dumps(stats)


def test_api_reports_a_screening_that_cannot_run(store: Store) -> None:
    def broken(_: Store) -> dict[str, int]:
        raise FileNotFoundError("could not find role_rules.json")

    response = Api(store, lambda urls, expand=False: [], screener=broken).handle("POST", "/api/screen")
    assert response.status == 500 and "role_rules.json" in response.payload["error"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_screen_command(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.db"
    with Store(db) as store:
        seed(store)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(PROFILE))

    assert main(["screen", "--db", str(db), "--profile", str(profile)]) == 0

    out = capsys.readouterr().out
    assert f"{len(JOBS)} postings screened." in out
    assert "2  model" in out
    with Store(db) as store:
        assert store.count_postings(stage=["model"]) == 2
