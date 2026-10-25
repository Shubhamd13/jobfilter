"""The screen-and-read background job and its endpoint. No model is called."""

from __future__ import annotations

import json
import threading

import pytest

from jobfilter.roles import Profile, ReadSummary, RoleRules, read_store, screen_store
from jobfilter.storage import Store
from jobfilter.web import Api, make_store_reader

from .test_applications import JOBS, seed
from .test_reader import PROFILE_WITH_FIT, FakeReader
from .test_roles import PROFILE

profile = Profile.from_dict(PROFILE_WITH_FIT)


def screener(store: Store) -> dict[str, int]:
    return screen_store(store, RoleRules.load(), profile)


def reading(store: Store, progress) -> ReadSummary:
    return read_store(store, FakeReader(), rules=RoleRules.load(), profile=profile, progress=progress)


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as opened:
        yield opened


def api(store: Store, reader=reading) -> Api:
    return Api(store, lambda urls, expand=False: [], screener=screener, reader=reader)


def test_new_postings_are_reported_as_waiting(store: Store) -> None:
    seed(store)
    payload = api(store).handle("GET", "/api/process").payload

    assert payload["waiting"] == {"unscreened": len(JOBS), "unread": 0}
    assert payload["can_read"] is True
    assert payload["process"]["running"] is False and payload["process"]["finished_at"] is None


def test_the_job_screens_then_reads(store: Store) -> None:
    seed(store)
    served = api(store)

    started = served.handle("POST", "/api/process")
    assert started.status == 202
    served.process_job.join(timeout=10)

    payload = served.handle("GET", "/api/process").payload
    run = payload["process"]
    assert run["running"] is False and run["error"] is None and run["finished_at"]
    assert run["stages"]["model"] == 2 and (run["read"], run["failed"], run["stopped"]) == (2, 0, None)
    assert (run["done"], run["total"]) == (2, 2), "progress reached the end"
    assert payload["waiting"] == {"unscreened": 0, "unread": 0}
    assert {r["job_id"]: r["verdict"] for r in store.postings(stage=["model"])} == {"swe": "fit", "senior": "skip"}
    json.dumps(payload)


def test_only_one_job_runs_at_a_time(store: Store) -> None:
    seed(store)
    release = threading.Event()

    def slow(store: Store, progress) -> ReadSummary:
        release.wait(timeout=10)
        return reading(store, progress)

    served = api(store, reader=slow)
    assert served.handle("POST", "/api/process").status == 202
    try:
        again = served.handle("POST", "/api/process")
        assert again.status == 409
        assert again.payload["process"]["running"] is True, "the running job's progress still comes back"
    finally:
        release.set()
        served.process_job.join(timeout=10)
    assert served.handle("POST", "/api/process").status == 202, "and a finished job can be followed by another"
    served.process_job.join(timeout=10)


def test_a_server_without_a_reader_only_screens(store: Store) -> None:
    seed(store)
    served = api(store, reader=None)
    served.handle("POST", "/api/process")
    served.process_job.join(timeout=10)

    payload = served.handle("GET", "/api/process").payload
    assert payload["can_read"] is False
    assert payload["process"]["stages"]["model"] == 2 and payload["process"]["read"] == 0
    assert payload["waiting"] == {"unscreened": 0, "unread": 2}


def test_a_job_that_dies_says_why(store: Store) -> None:
    def broken(store: Store, progress) -> ReadSummary:
        raise RuntimeError("the GPU caught fire")

    served = api(store, reader=broken)
    served.handle("POST", "/api/process")
    served.process_job.join(timeout=10)
    run = served.handle("GET", "/api/process").payload["process"]
    assert run["running"] is False and run["error"] == "RuntimeError: the GPU caught fire"


def test_a_model_that_is_down_is_reported_not_raised(store: Store) -> None:
    seed(store)

    def down(store: Store, progress) -> ReadSummary:
        return read_store(store, FakeReader(fail=True), rules=RoleRules.load(), profile=profile, progress=progress)

    served = api(store, reader=down)
    served.handle("POST", "/api/process")
    served.process_job.join(timeout=10)
    payload = served.handle("GET", "/api/process").payload
    assert payload["process"]["error"] is None
    assert payload["process"]["read"] == 0 and payload["waiting"]["unread"] == 2


def test_the_real_reader_needs_a_fit_section(store: Store, tmp_path, monkeypatch) -> None:
    bare = tmp_path / "profile.json"
    bare.write_text(json.dumps(PROFILE))
    monkeypatch.setenv("JOBFILTER_PROFILE", str(bare))

    served = api(store, reader=make_store_reader())
    served.handle("POST", "/api/process")
    served.process_job.join(timeout=10)
    assert '"fit" section' in served.handle("GET", "/api/process").payload["process"]["error"]
