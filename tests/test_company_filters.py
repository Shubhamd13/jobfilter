"""The per-company table counted under the postings filters."""

from __future__ import annotations

import dataclasses

import pytest

from jobfilter.models import Label
from jobfilter.storage import Store
from jobfilter.web import Api

from .test_storage import make_posting, make_result

# (company, job id, location, label)
POSTINGS = [
    ("acme", "a1", "Austin, TX", Label.OPEN),
    ("acme", "a2", "London", Label.OPEN),
    ("acme", "a3", "Austin, TX", Label.RESTRICTED_CITIZEN),
    ("beta", "b1", "Boston, MA", Label.OPEN),
    ("beta", "b2", "Boston, MA", Label.UNCLEAR),
    ("gamma", "g1", "Berlin", Label.RESTRICTED_CLEARANCE),
]


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as opened:
        opened.record_run(
            "t",
            [
                make_result(
                    dataclasses.replace(make_posting(slug=slug, job_id=job_id), location=location),
                    label=label,
                )
                for slug, job_id, location, label in POSTINGS
            ],
        )
        yield opened


def by_company(rows: list[dict]) -> dict[str, dict]:
    return {row["company_slug"]: row for row in rows}


def test_unfiltered_counts_are_the_whole_company(store: Store) -> None:
    rows = by_company(store.company_summary())
    assert (rows["acme"]["total"], rows["acme"]["restricted"], rows["acme"]["open_count"]) == (3, 1, 2)
    assert all(row["total"] == row["all_total"] for row in rows.values())


@pytest.mark.parametrize(
    "filters,expected",
    [
        # company -> (total, restricted, unclear, open)
        ({"label": ["OPEN"]}, {"acme": (2, 0, 0, 2), "beta": (1, 0, 0, 1), "gamma": (0, 0, 0, 0)}),
        ({"region": ["us"]}, {"acme": (2, 1, 0, 1), "beta": (2, 0, 1, 1), "gamma": (0, 0, 0, 0)}),
        ({"region": ["us"], "label": ["OPEN", "UNCLEAR"]}, {"acme": (1, 0, 0, 1), "beta": (2, 0, 1, 1), "gamma": (0, 0, 0, 0)}),
        ({"place": ["Berlin"]}, {"acme": (0, 0, 0, 0), "beta": (0, 0, 0, 0), "gamma": (1, 1, 0, 0)}),
    ],
    ids=["label", "region", "region-and-label", "place"],
)
def test_counts_follow_the_filters(store: Store, filters: dict, expected: dict) -> None:
    rows = by_company(store.company_summary(**filters))
    found = {
        slug: (row["total"], row["restricted"], row["unclear"], row["open_count"])
        for slug, row in rows.items()
    }
    assert found == expected
    assert {slug: row["all_total"] for slug, row in rows.items()} == {"acme": 3, "beta": 2, "gamma": 1}


def test_every_company_stays_listed_and_sorted_by_what_matches(store: Store) -> None:
    rows = store.company_summary(label=["OPEN"])
    assert [row["company_slug"] for row in rows] == ["acme", "beta", "gamma"]
    assert rows[-1]["total"] == 0, "a company with nothing matching is still there, last"


def test_company_filters_do_not_narrow_the_company_table(store: Store) -> None:
    """Hiding a company on Explore must not drop it from the table that refreshes it."""
    hidden = by_company(store.company_summary(exclude_companies=["acme"], company=["beta"]))
    assert hidden["acme"]["total"] == 3 and hidden["gamma"]["total"] == 1


def test_marks_and_stages_narrow_the_counts_too(store: Store) -> None:
    first = store.postings(company=["acme"])[0]["posting_id"]
    store.set_application(first, "applied")
    rows = by_company(store.company_summary(app_status=["applied"]))
    assert (rows["acme"]["total"], rows["beta"]["total"]) == (1, 0)


def test_api_companies_takes_the_postings_filters(store: Store) -> None:
    api = Api(store, lambda urls, expand=False: [])

    everything = by_company(api.handle("GET", "/api/companies").payload["companies"])
    assert everything["acme"]["total"] == 3 and "refreshable" in everything["acme"]

    us_open = api.handle("GET", "/api/companies", {"region": ["us"], "label": ["OPEN"]})
    assert us_open.status == 200
    rows = by_company(us_open.payload["companies"])
    assert {slug: row["total"] for slug, row in rows.items()} == {"acme": 1, "beta": 1, "gamma": 0}
    assert rows["acme"]["all_total"] == 3
