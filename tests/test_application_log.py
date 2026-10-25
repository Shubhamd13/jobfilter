"""The Applications page's data: applied postings by where they stand and by
resume, and what came of the applications each resume went to.

Storage and the API on top of it. The page is exercised in
test_web_ui_applications.py.
"""

from __future__ import annotations

import pytest

from jobfilter.storage import NO_RESUME, WAITING, Store
from jobfilter.web import Api

from .test_applications import JOBS, seed
from .test_resumes import OTHER_PDF, PDF

ZERO = {"sent": 0, "waiting": 0, "oa": 0, "interview": 0, "offer": 0, "rejected": 0, "replied": 0}


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as opened:
        yield opened


def applications(store: Store) -> dict[str, int]:
    """Five applications over two resumes, and two postings not applied to.

        swe        sim         OA, then rejected
        senior     sim         interview
        mech       sim         (waiting)
        staff      perception  rejected
        cleared    no resume   (waiting)
        recruiter  maybe, with the sim resume left on it from before
        london     not marked
    """
    ids = seed(store)
    sim, _ = store.add_resume("sim", "sim.pdf", "application/pdf", PDF)
    perception, _ = store.add_resume("perception", "perception.pdf", "application/pdf", OTHER_PDF)
    for job in ("swe", "senior", "mech"):
        store.set_application(ids[job], "applied", resume_id=sim["id"])
    store.set_application(ids["staff"], "applied", resume_id=perception["id"])
    store.set_application(ids["cleared"], "applied")
    store.set_application(ids["recruiter"], "maybe", resume_id=sim["id"])

    store.set_outcome(ids["swe"], "oa")
    store.set_outcome(ids["swe"], "rejected")
    store.set_outcome(ids["senior"], "interview")
    store.set_outcome(ids["staff"], "rejected")
    return {**ids, "sim": sim["id"], "perception": perception["id"]}


def jobs(store: Store, ids: dict[str, int], **filters) -> set[str]:
    by_id = {ids[job]: job for job in JOBS}  # `ids` also names the two resumes
    return {by_id[row["posting_id"]] for row in store.postings(**filters)}


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (["rejected"], {"swe", "staff"}),
        (["interview"], {"senior"}),
        (["oa"], set()),
        (["offer"], set()),
        ([WAITING], {"mech", "cleared"}),
        ([WAITING, "interview"], {"mech", "cleared", "senior"}),
    ],
    ids=["rejected", "interview", "oa-moved-on", "offer", "waiting", "waiting-or-interview"],
)
def test_filter_by_where_it_stands(store: Store, outcome: list[str], expected: set[str]) -> None:
    ids = applications(store)
    assert jobs(store, ids, outcome=outcome) == expected
    assert store.count_postings(outcome=outcome) == len(expected)


def test_waiting_is_applied_postings_only(store: Store) -> None:
    """A posting never marked has no outcome either; it is not waiting on anyone."""
    ids = applications(store)
    assert {"london", "recruiter"}.isdisjoint(jobs(store, ids, outcome=[WAITING]))


def test_filter_by_resume(store: Store) -> None:
    ids = applications(store)
    applied = {"app_status": ["applied"]}
    assert jobs(store, ids, resume=[str(ids["sim"])], **applied) == {"swe", "senior", "mech"}
    assert jobs(store, ids, resume=[str(ids["perception"])], **applied) == {"staff"}
    assert jobs(store, ids, resume=[NO_RESUME], **applied) == {"cleared"}
    assert jobs(store, ids, resume=[NO_RESUME, str(ids["perception"])], **applied) == {"staff", "cleared"}
    assert jobs(store, ids, resume=["999"], **applied) == set()
    assert jobs(store, ids, resume=["not-an-id"], **applied) == set()


def test_filters_combine(store: Store) -> None:
    ids = applications(store)
    assert jobs(store, ids, resume=[str(ids["sim"])], outcome=["rejected"]) == {"swe"}
    assert jobs(store, ids, resume=[str(ids["sim"])], outcome=[WAITING]) == {"mech"}


def test_counts_by_where_it_stands(store: Store) -> None:
    ids = applications(store)
    counts = store.filtered_outcome_counts()
    assert counts == {"waiting": 2, "oa": 0, "interview": 1, "offer": 0, "rejected": 2}
    assert sum(counts.values()) == store.count_postings(app_status=["applied"])

    # They follow every filter but their own.
    assert store.filtered_outcome_counts(outcome=["rejected"]) == counts
    assert store.filtered_outcome_counts(resume=[str(ids["sim"])]) == {
        "waiting": 1, "oa": 0, "interview": 1, "offer": 0, "rejected": 1,
    }


def test_counts_on_an_empty_database(store: Store) -> None:
    assert store.filtered_outcome_counts() == {
        "waiting": 0, "oa": 0, "interview": 0, "offer": 0, "rejected": 0,
    }
    assert store.resume_results() == {}


# --------------------------------------------------------------------------
# What came of each resume
# --------------------------------------------------------------------------

def test_results_per_resume(store: Store) -> None:
    ids = applications(store)
    results = store.resume_results()
    assert results == {
        # swe reached an OA and was rejected: it is in both, and it got a reply.
        ids["sim"]: {"sent": 3, "waiting": 1, "oa": 1, "interview": 1, "offer": 0, "rejected": 1, "replied": 2},
        ids["perception"]: {**ZERO, "sent": 1, "rejected": 1},
        None: {**ZERO, "sent": 1, "waiting": 1},
    }


def test_only_applied_postings_count_for_a_resume(store: Store) -> None:
    """The Maybe posting still carries the sim resume; it was never sent."""
    ids = applications(store)
    assert store.resume_results()[ids["sim"]]["sent"] == 3
    used = {r["id"]: r["used"] for r in store.resumes()}
    assert used[ids["sim"]] == 4, "but it is in use, so it cannot be deleted"


def test_a_resume_sent_nowhere_has_no_results(store: Store) -> None:
    resume, _ = store.add_resume("sim", "sim.pdf", "application/pdf", PDF)
    assert resume["id"] not in store.resume_results()


def test_results_follow_a_step_taken_back(store: Store) -> None:
    ids = applications(store)
    store.set_outcome(ids["senior"], "interview", on=False)
    sim = store.resume_results()[ids["sim"]]
    assert (sim["waiting"], sim["interview"], sim["replied"]) == (2, 0, 1)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@pytest.fixture
def api(store: Store) -> Api:
    return Api(store, lambda urls, expand=False: [])


def test_api_narrows_the_log(api: Api, store: Store) -> None:
    ids = applications(store)
    page = api.handle(
        "GET", "/api/postings",
        query={"app": ["applied"], "outcome": ["rejected"], "resume": [str(ids["sim"])]},
    ).payload
    assert [p["posting_id"] for p in page["postings"]] == [ids["swe"]]
    assert page["total"] == 1
    assert page["outcome_counts"] == {"waiting": 1, "oa": 0, "interview": 1, "offer": 0, "rejected": 1}
    row = page["postings"][0]
    assert row["resume_name"] == "sim" and row["outcome_steps"].startswith("oa:")


def test_api_counts_every_application_without_filters(api: Api, store: Store) -> None:
    applications(store)
    counts = api.handle("GET", "/api/postings", query={"app": ["applied"]}).payload["outcome_counts"]
    assert sum(counts.values()) == 5


def test_api_lists_resumes_with_their_results(api: Api, store: Store) -> None:
    ids = applications(store)
    payload = api.handle("GET", "/api/resumes").payload
    assert {r["name"] for r in payload["resumes"]} == {"sim", "perception"}
    assert payload["results"] == {
        str(ids["sim"]): {"sent": 3, "waiting": 1, "oa": 1, "interview": 1, "offer": 0, "rejected": 1, "replied": 2},
        str(ids["perception"]): {**ZERO, "sent": 1, "rejected": 1},
        "none": {**ZERO, "sent": 1, "waiting": 1},
    }


def test_api_resumes_before_anything_is_uploaded(api: Api) -> None:
    assert api.handle("GET", "/api/resumes").payload == {"resumes": [], "results": {}}
