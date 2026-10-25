"""SQLite storage. Offline: everything runs against :memory: or tmp_path."""

from __future__ import annotations

import dataclasses

import json
import re
import sqlite3
from pathlib import Path

import pytest

from jobfilter.models import Change, Label, Origin, Platform, Posting, Result, Status
from jobfilter.storage import SCHEMA_VERSION, Store, content_hash, default_db_path
from jobfilter.storage.store import DB_ENV_VAR, company_name


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as store:
        yield store


def make_posting(
    *,
    slug: str = "acme",
    job_id: str = "j1",
    title: str = "Engineer",
    text: str = "We are hiring.",
    raw: dict | None = None,
    platform: Platform = Platform.LEVER,
) -> Posting:
    return Posting(
        platform=platform,
        company_slug=slug,
        job_id=job_id,
        title=title,
        location="Remote",
        url=f"https://jobs.lever.co/{slug}/{job_id}",
        description_text=text,
        raw=raw or {"id": job_id},
    )


def make_result(
    posting: Posting | None = None,
    *,
    label: Label = Label.OPEN,
    status: Status = Status.OK,
    evidence: str | None = None,
    matched_rule: str | None = None,
    url: str | None = None,
    error: str | None = None,
    origin: Origin = Origin.REQUESTED,
) -> Result:
    return Result(
        url=url or (posting.url if posting else "https://example.com/x"),
        status=status,
        platform=posting.platform if posting else Platform.UNSUPPORTED,
        company_slug=posting.company_slug if posting else None,
        job_id=posting.job_id if posting else None,
        title=posting.title if posting else None,
        location=posting.location if posting else None,
        label=label,
        confidence=1.0 if status is Status.OK else None,
        evidence=evidence,
        matched_rule=matched_rule,
        matched_rules=(matched_rule,) if matched_rule else (),
        classifier_name="rules" if status is Status.OK else None,
        error=error,
        error_type="JobNotFound" if error else None,
        origin=origin,
        posting=posting,
    )


# --------------------------------------------------------------------------
# Schema and migration
# --------------------------------------------------------------------------

def test_a_new_database_is_at_the_current_version(store: Store) -> None:
    assert store.version == SCHEMA_VERSION


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with Store(path) as first:
        first.upsert_posting(make_posting())
    with Store(path) as second:
        assert second.migrate() == SCHEMA_VERSION
        assert second.stats()["postings"] == 1


def _before_refresh(schema: str) -> str:
    """schema.sql as it was before version 4: no closed_at, no refresh tables."""
    schema = re.sub(r"\n\s+(?:p\.)?closed_at\b[^\n]*", "", schema)
    return schema + "\nDROP TABLE posting_changes;\nDROP TABLE board_refreshes;\n"


def _without_listing_columns(schema: str) -> str:
    """schema.sql as it was before version 3 added the listing signals."""
    schema = _before_refresh(schema)
    for column in ("posted_ago", "applicants", "listing_seen_at"):
        schema = re.sub(rf"\n\s+(?:p\.)?{column}\b[^\n]*", "", schema)
    return schema


def test_a_v2_database_gains_the_listing_columns(tmp_path: Path) -> None:
    """Schema 3 added postings.posted_ago / applicants / listing_seen_at."""
    path = tmp_path / "v2.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        _without_listing_columns(Path("jobfilter/storage/schema.sql").read_text())
    )
    legacy.execute("PRAGMA user_version = 2")
    legacy.execute(
        "INSERT INTO companies (platform, slug, first_seen, last_seen)"
        " VALUES ('linkedin', 'acme', 'then', 'then')"
    )
    legacy.execute(
        "INSERT INTO postings (company_id, job_id, url, description_text,"
        " content_hash, first_seen, last_fetched)"
        " VALUES (1, '42', 'https://old/url', 'text', 'h', 'then', 'then')"
    )
    legacy.commit()
    legacy.close()

    with Store(path) as store:
        assert store.version == SCHEMA_VERSION
        row = store.connection.execute("SELECT * FROM posting_overview").fetchone()
        assert row["url"] == "https://old/url"
        assert row["posted_ago"] is None and row["applicants"] is None


def test_a_v3_database_gains_closed_at_and_the_refresh_tables(tmp_path: Path) -> None:
    """Schema 4 added postings.closed_at, posting_changes and board_refreshes."""
    path = tmp_path / "v3.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(_before_refresh(Path("jobfilter/storage/schema.sql").read_text()))
    legacy.execute("PRAGMA user_version = 3")
    legacy.execute(
        "INSERT INTO companies (platform, slug, first_seen, last_seen)"
        " VALUES ('lever', 'acme', 'then', 'then')"
    )
    legacy.execute(
        "INSERT INTO postings (company_id, job_id, url, description_text,"
        " content_hash, first_seen, last_fetched)"
        " VALUES (1, '42', 'https://old/url', 'text', 'h', 'then', 'then')"
    )
    legacy.commit()
    columns = {row[1] for row in legacy.execute("PRAGMA table_info(postings)")}
    assert "closed_at" not in columns  # the legacy file really is a v3 one
    legacy.close()

    with Store(path) as store:
        assert store.version == SCHEMA_VERSION
        row = store.connection.execute("SELECT * FROM posting_overview").fetchone()
        assert row["url"] == "https://old/url"
        assert row["closed_at"] is None and row["changes"] is None
        assert store.refresh_summary() == {}
        assert [b.slug for b in store.board_companies([Platform.LEVER])] == ["acme"]


def test_a_v1_database_upgrades_in_place_without_losing_data(
    tmp_path: Path,
) -> None:
    """Schema 2 added run_items.origin; existing rows must survive."""
    path = tmp_path / "v1.db"

    # Build a v1 database: the v2 schema minus the column, stamped as v1.
    v1_sql = (
        Path("jobfilter/storage/schema.sql")
        .read_text()
        .replace("    origin            TEXT NOT NULL DEFAULT 'requested',\n", "")
    )
    v1_sql = _without_listing_columns(v1_sql)
    legacy = sqlite3.connect(path)
    legacy.executescript(v1_sql)
    legacy.execute("PRAGMA user_version = 1")
    legacy.execute(
        "INSERT INTO runs (source, n_urls, started_at) VALUES ('old', 1, 'then')"
    )
    legacy.execute(
        "INSERT INTO run_items (run_id, url_as_given, status, label, created_at)"
        " VALUES (1, 'https://old/url', 'ok', 'OPEN', 'then')"
    )
    legacy.commit()
    legacy.close()

    with Store(path) as store:
        assert store.version == SCHEMA_VERSION
        columns = [
            r[1] for r in store.connection.execute("PRAGMA table_info(run_items)")
        ]
        assert "origin" in columns
        row = store.connection.execute("SELECT * FROM run_items").fetchone()
        assert row["url_as_given"] == "https://old/url"
        assert row["origin"] == "requested", "existing rows get the default"


def test_origin_is_stored_and_surfaced(store: Store) -> None:
    store.record_run(
        "test",
        [
            make_result(make_posting(job_id="asked"), origin=Origin.REQUESTED),
            make_result(make_posting(job_id="found"), origin=Origin.DISCOVERED),
        ],
    )
    origins = {r["job_id"]: r["origin"] for r in store.postings()}
    assert origins == {"asked": "requested", "found": "discovered"}


def test_the_database_file_is_created_with_parents(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deep" / "jobfilter.db"
    with Store(path):
        pass
    assert path.is_file()


def test_foreign_keys_are_enforced(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO postings (company_id, job_id, url, description_text,"
            " content_hash, first_seen, last_fetched)"
            " VALUES (9999, 'x', 'u', 't', 'h', 'now', 'now')"
        )


def test_deleting_a_company_cascades(store: Store) -> None:
    posting_id = store.upsert_posting(make_posting())
    store.record_classification(posting_id, make_result(make_posting()))
    store.connection.execute("DELETE FROM companies")
    store.connection.commit()
    assert store.stats()["postings"] == 0
    assert store._scalar("SELECT COUNT(*) FROM classifications") == 0


def test_default_db_path_honours_the_env_var(monkeypatch) -> None:
    monkeypatch.setenv(DB_ENV_VAR, "/tmp/somewhere/else.db")
    assert default_db_path() == Path("/tmp/somewhere/else.db")
    monkeypatch.delenv(DB_ENV_VAR)
    assert default_db_path().name == "jobfilter.db"


# --------------------------------------------------------------------------
# Postings accumulate rather than duplicate
# --------------------------------------------------------------------------

def test_upsert_creates_company_and_posting(store: Store) -> None:
    store.upsert_posting(make_posting(slug="acme", job_id="j1"))
    assert store.stats()["postings"] == 1
    assert store.stats()["companies"] == 1
    assert store.companies() == ["acme"]


def test_rechecking_the_same_posting_does_not_duplicate_it(store: Store) -> None:
    first = store.upsert_posting(make_posting())
    second = store.upsert_posting(make_posting())
    assert first == second
    assert store.stats()["postings"] == 1


def test_recheck_keeps_first_seen_and_moves_last_fetched(store: Store) -> None:
    posting_id = store.upsert_posting(make_posting())
    before = store.postings()[0]

    store.connection.execute(
        "UPDATE postings SET first_seen = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (posting_id,),
    )
    store.connection.commit()
    store.upsert_posting(make_posting(text="Updated text."))

    after = store.postings()[0]
    assert after["first_seen"] == "2020-01-01T00:00:00+00:00"
    assert after["last_fetched"] >= before["last_fetched"]


def test_recheck_updates_the_content_hash_when_the_text_changed(store: Store) -> None:
    store.upsert_posting(make_posting(text="Original."))
    original = store.postings()[0]["content_hash"]

    store.upsert_posting(make_posting(text="Now requires U.S. citizenship."))
    assert store.postings()[0]["content_hash"] != original
    assert store.postings()[0]["content_hash"] == content_hash(
        "Now requires U.S. citizenship."
    )


def test_two_jobs_at_one_company_share_a_company_row(store: Store) -> None:
    store.upsert_posting(make_posting(job_id="j1"))
    store.upsert_posting(make_posting(job_id="j2"))
    stats = store.stats()
    assert stats["postings"] == 2
    assert stats["companies"] == 1


def test_company_name_is_taken_from_greenhouse_raw(store: Store) -> None:
    posting = make_posting(
        slug="undercontrolroboticsinc",
        platform=Platform.GREENHOUSE,
        raw={"company_name": "Noble Machines, Inc"},
    )
    assert company_name(posting) == "Noble Machines, Inc"

    store.upsert_posting(posting, company_name(posting))
    assert store.postings()[0]["company_name"] == "Noble Machines, Inc"


def test_company_name_is_none_when_the_platform_does_not_give_one(store: Store) -> None:
    assert company_name(make_posting()) is None


# --------------------------------------------------------------------------
# Classification history
# --------------------------------------------------------------------------

def test_an_unchanged_recheck_does_not_grow_the_history(store: Store) -> None:
    posting = make_posting()
    posting_id = store.upsert_posting(posting)

    first = store.record_classification(posting_id, make_result(posting))
    second = store.record_classification(posting_id, make_result(posting))

    assert first == second
    assert len(store.history(posting_id)) == 1


def test_a_changed_label_appends_to_the_history(store: Store) -> None:
    posting = make_posting()
    posting_id = store.upsert_posting(posting)
    store.record_classification(posting_id, make_result(posting, label=Label.OPEN))

    edited = make_posting(text="Now requires U.S. citizenship.")
    store.upsert_posting(edited)
    store.record_classification(
        posting_id,
        make_result(edited, label=Label.RESTRICTED_CITIZEN, evidence="…citizenship."),
    )

    history = store.history(posting_id)
    assert [h["label"] for h in history] == ["OPEN", "RESTRICTED_CITIZEN"]
    assert history[0]["content_hash"] != history[1]["content_hash"]


def test_the_overview_shows_only_the_latest_classification(store: Store) -> None:
    posting = make_posting()
    posting_id = store.upsert_posting(posting)
    store.record_classification(posting_id, make_result(posting, label=Label.OPEN))
    store.record_classification(
        posting_id, make_result(posting, label=Label.RESTRICTED_CLEARANCE)
    )

    rows = store.postings()
    assert len(rows) == 1
    assert rows[0]["label"] == "RESTRICTED_CLEARANCE"


def test_different_classifiers_each_keep_their_own_latest(store: Store) -> None:
    """Phase 2b compares a model verdict against the rule verdict."""
    posting = make_posting()
    posting_id = store.upsert_posting(posting)

    rules = make_result(posting, label=Label.UNCLEAR)
    store.record_classification(posting_id, rules)

    model = make_result(posting, label=Label.RESTRICTED_CITIZEN)
    object.__setattr__(model, "classifier_name", "ollama")
    store.record_classification(posting_id, model)

    assert {h["classifier_name"] for h in store.history(posting_id)} == {
        "rules",
        "ollama",
    }
    assert store.postings(classifier="rules")[0]["label"] == "UNCLEAR"
    assert store.postings(classifier="ollama")[0]["label"] == "RESTRICTED_CITIZEN"


def test_matched_rules_round_trip_as_json(store: Store) -> None:
    posting = make_posting()
    posting_id = store.upsert_posting(posting)
    store.record_classification(
        posting_id, make_result(posting, matched_rule="must-be-us-citizen")
    )
    stored = store.history(posting_id)[0]["matched_rules_json"]
    assert json.loads(stored) == ["must-be-us-citizen"]


# --------------------------------------------------------------------------
# Runs and failures
# --------------------------------------------------------------------------

def test_record_run_stores_successes_and_failures(store: Store) -> None:
    results = [
        make_result(make_posting(job_id="j1"), label=Label.RESTRICTED_CITIZEN),
        make_result(
            None,
            status=Status.UNSUPPORTED,
            label=Label.UNCLEAR,
            url="https://linkedin.com/x",
            error="unsupported",
        ),
    ]
    store.record_run("urls.txt", results)

    stats = store.stats()
    assert stats["runs"] == 1
    assert stats["postings"] == 1
    assert stats["errors"] == 1
    assert store._scalar("SELECT COUNT(*) FROM run_items") == 2


def test_a_failure_creates_no_posting(store: Store) -> None:
    run_id = store.start_run("args", 1)
    posting_id = store.record_result(
        run_id,
        make_result(None, status=Status.FETCH_ERROR, label=Label.UNCLEAR, error="gone"),
    )
    assert posting_id is None
    assert store.stats()["postings"] == 0


def test_failures_are_listed_with_their_error(store: Store) -> None:
    store.record_run(
        "args",
        [
            make_result(
                None,
                status=Status.FETCH_ERROR,
                label=Label.UNCLEAR,
                url="https://jobs.lever.co/acme/nope",
                error="job not found",
            )
        ],
    )
    failure = store.failures()[0]
    assert failure["url_as_given"] == "https://jobs.lever.co/acme/nope"
    assert failure["error_type"] == "JobNotFound"
    assert failure["status"] == "fetch_error"


def test_a_failure_fetched_since_is_no_longer_listed(store: Store) -> None:
    posting = make_posting(job_id="j9")
    store.record_run("args", [make_result(None, status=Status.FETCH_ERROR, url=posting.url, error="rate limited")])
    store.record_run("args", [make_result(None, status=Status.FETCH_ERROR, url="https://jobs.lever.co/acme/other", error="gone")])
    assert len(store.failures()) == 2

    store.record_run("refresh", [make_result(posting)])
    assert [f["url_as_given"] for f in store.failures()] == ["https://jobs.lever.co/acme/other"]


def test_a_stored_job_that_fails_later_is_listed(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    posting = make_posting(job_id="j9")
    monkeypatch.setattr("jobfilter.storage.store.utcnow", lambda: "2026-10-01T00:00:00+00:00")
    store.record_run("args", [make_result(posting)])
    monkeypatch.setattr("jobfilter.storage.store.utcnow", lambda: "2026-10-02T00:00:00+00:00")
    store.record_run("refresh", [make_result(None, status=Status.FETCH_ERROR, url=posting.url, error="gone")])
    assert [f["url_as_given"] for f in store.failures()] == [posting.url]


def test_a_run_is_marked_finished(store: Store) -> None:
    store.record_run("urls.txt", [make_result(make_posting())])
    row = store.connection.execute("SELECT * FROM runs").fetchone()
    assert row["finished_at"] is not None
    assert row["n_urls"] == 1


def test_two_runs_of_the_same_urls_keep_one_posting(store: Store) -> None:
    for _ in range(2):
        store.record_run("urls.txt", [make_result(make_posting())])
    stats = store.stats()
    assert stats["runs"] == 2
    assert stats["postings"] == 1, "two runs, one posting"
    assert store._scalar("SELECT COUNT(*) FROM run_items") == 2


# --------------------------------------------------------------------------
# Queries the UI will make
# --------------------------------------------------------------------------

@pytest.fixture
def populated(store: Store) -> Store:
    store.record_run(
        "urls.txt",
        [
            make_result(
                make_posting(slug="acme", job_id="j1", title="Robotics Engineer"),
                label=Label.RESTRICTED_CITIZEN,
                evidence="Must be a U.S. citizen.",
                matched_rule="must-be-us-citizen",
            ),
            make_result(
                make_posting(slug="acme", job_id="j2", title="Data Scientist"),
                label=Label.OPEN,
            ),
            make_result(
                make_posting(slug="beta", job_id="j3", title="ML Intern"),
                label=Label.UNCLEAR,
            ),
            make_result(
                None,
                status=Status.UNSUPPORTED,
                label=Label.UNCLEAR,
                url="https://linkedin.com/x",
                error="unsupported",
            ),
        ],
    )
    return store


def test_postings_are_newest_first(populated: Store) -> None:
    rows = populated.postings()
    assert len(rows) == 3
    assert rows[0]["posting_id"] >= rows[-1]["posting_id"]


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        pytest.param({"label": "RESTRICTED_CITIZEN"}, 1, id="by-label"),
        pytest.param({"label": "OPEN"}, 1, id="by-label-open"),
        pytest.param({"company": "acme"}, 2, id="by-company"),
        pytest.param({"company": "nobody"}, 0, id="by-missing-company"),
        pytest.param({"query": "Robotics"}, 1, id="search-title"),
        pytest.param({"query": "citizen"}, 1, id="search-evidence"),
        pytest.param({"query": "beta"}, 1, id="search-company"),
        pytest.param({"limit": 2}, 2, id="limit"),
        pytest.param({"limit": 2, "offset": 2}, 1, id="offset"),
    ],
)
def test_posting_filters(populated: Store, kwargs: dict, expected: int) -> None:
    assert len(populated.postings(**kwargs)) == expected


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        pytest.param({"label": ["RESTRICTED_CITIZEN", "UNCLEAR"]}, 2, id="several-labels"),
        pytest.param({"company": ["acme", "beta"]}, 3, id="several-companies"),
        pytest.param({"exclude_companies": ["acme"]}, 1, id="exclude-one"),
        pytest.param({"exclude_companies": ["acme", "beta"]}, 0, id="exclude-all"),
        pytest.param({"exclude_companies": ["nobody"]}, 3, id="exclude-unknown"),
        pytest.param({"exclude_companies": ["", "  "]}, 3, id="blank-excludes-ignored"),
        pytest.param({"label": []}, 3, id="empty-list-is-no-filter"),
        pytest.param(
            {"label": "OPEN", "exclude_companies": ["beta"]}, 1, id="label-and-exclude"
        ),
        pytest.param(
            {"company": ["acme"], "exclude_companies": ["acme"]}, 0, id="exclude-wins"
        ),
    ],
)
def test_multi_value_filters(populated: Store, kwargs: dict, expected: int) -> None:
    assert len(populated.postings(**kwargs)) == expected
    assert populated.count_postings(**kwargs) == expected


def test_count_ignores_the_page(populated: Store) -> None:
    assert len(populated.postings(limit=1)) == 1
    assert populated.count_postings() == 3


def test_search_matches_the_company_name(store: Store) -> None:
    posting = make_posting(
        slug="andurilindustries", job_id="a1", raw={"company_name": "Anduril Industries"}
    )
    store.record_run("t", [make_result(posting, label=Label.OPEN)])
    assert store.count_postings(query="Anduril") == 1


def test_filtered_label_counts_ignore_the_label_filter(populated: Store) -> None:
    counts = populated.filtered_label_counts(exclude_companies=["beta"])
    assert counts["RESTRICTED_CITIZEN"] == 1
    assert counts["OPEN"] == 1
    assert counts["UNCLEAR"] == 0, "beta's posting is hidden"
    assert set(counts) == {str(label) for label in Label}


@pytest.fixture
def located(store: Store) -> Store:
    locations = {
        "austin": "Austin, Texas, United States; South San Francisco, California, United States",
        "abu1": "Abu Dhabi",
        "abu2": "Abu Dhabi, United Arab Emirates; London, England, United Kingdom",
        "both": "Seattle, WA; London",
        "remote": "Remote",
        "odd": "100% remote_ok",
        "bay": "San Francisco Bay Area",
    }
    store.record_run(
        "t",
        [
            make_result(
                dataclasses.replace(make_posting(job_id=job_id), location=location),
                label=Label.OPEN,
            )
            for job_id, location in locations.items()
        ],
    )
    return store


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        pytest.param({"region": "us"}, {"austin", "both", "bay"}, id="us"),
        pytest.param({"region": "outside_us"}, {"abu1", "abu2", "both"}, id="outside"),
        pytest.param({"region": "unknown"}, {"remote", "odd"}, id="unknown"),
        pytest.param(
            {"region": ["us", "unknown"]}, {"austin", "both", "bay", "remote", "odd"}, id="regions-or"
        ),
        pytest.param({"place": "USA"}, {"austin", "both", "bay"}, id="usa-means-the-region"),
        pytest.param({"place": "California"}, {"austin", "bay"}, id="state-means-its-cities"),
        pytest.param(
            {"exclude_places": "united states"}, {"abu1", "abu2", "remote", "odd"}, id="exclude-the-us"
        ),
        pytest.param({"place": "abu dhabi"}, {"abu1", "abu2"}, id="place-any-case-any-spelling"),
        pytest.param({"place": ["Abu Dhabi", "Austin"]}, {"abu1", "abu2", "austin"}, id="places-or"),
        pytest.param(
            {"exclude_places": "London"}, {"austin", "abu1", "remote", "odd", "bay"}, id="exclude-place"
        ),
        pytest.param(
            {"place": "Abu Dhabi", "exclude_places": "London"}, {"abu1"}, id="place-minus-place"
        ),
        pytest.param({"region": "us", "place": "London"}, {"both"}, id="region-and-place"),
        pytest.param({"place": "100%"}, {"odd"}, id="percent-is-literal"),
        pytest.param({"place": "Dhab_"}, set(), id="underscore-is-literal"),
        pytest.param({"query": "Abu Dhabi"}, {"abu1", "abu2"}, id="search-covers-location"),
    ],
)
def test_location_filters(located: Store, kwargs: dict, expected: set[str]) -> None:
    rows = located.postings(**kwargs)
    assert {r["job_id"] for r in rows} == expected
    assert located.count_postings(**kwargs) == len(expected)


def test_region_counts_ignore_the_region_filter(located: Store) -> None:
    counts = located.filtered_region_counts(region="us", place="London")
    assert counts == {"us": 1, "outside_us": 2, "unknown": 0}
    assert located.filtered_region_counts()["us"] == 3


def test_location_places_split_and_count(located: Store) -> None:
    places = {p["place"]: p["total"] for p in located.location_places()}
    assert places["London"] == 1
    assert places["London, England, United Kingdom"] == 1
    assert places["Abu Dhabi"] == 1
    assert places["South San Francisco, California, United States"] == 1
    assert len(places) == 10


def test_an_unknown_filter_name_is_an_error(store: Store) -> None:
    with pytest.raises(TypeError):
        store.postings(not_a_filter="x")


def test_company_summary_carries_the_display_name(store: Store) -> None:
    posting = make_posting(slug="acme", job_id="a1", raw={"company_name": "Acme Corp"})
    store.record_run("t", [make_result(posting, label=Label.OPEN)])
    assert store.company_summary()[0]["company_name"] == "Acme Corp"


def test_company_summary_counts_and_orders(populated: Store) -> None:
    summary = populated.company_summary()
    assert [c["company_slug"] for c in summary] == ["acme", "beta"]
    acme = summary[0]
    assert (acme["total"], acme["restricted"], acme["open_count"]) == (2, 1, 1)


def test_label_counts(populated: Store) -> None:
    counts = populated.label_counts()
    assert counts["RESTRICTED_CITIZEN"] == 1
    assert counts["OPEN"] == 1
    assert counts["UNCLEAR"] == 1
    assert counts["RESTRICTED_CLEARANCE"] == 0, "every label present, even at zero"


def test_stats_summarizes_the_whole_database(populated: Store) -> None:
    stats = populated.stats()
    assert stats["postings"] == 3
    assert stats["companies"] == 2
    assert stats["runs"] == 1
    assert stats["errors"] == 1
    assert stats["restricted"] == 1


def test_posting_detail_includes_text_and_history(populated: Store) -> None:
    posting_id = populated.postings(label="RESTRICTED_CITIZEN")[0]["posting_id"]
    detail = populated.posting(posting_id)
    assert detail is not None
    assert detail["description_text"] == "We are hiring."
    assert detail["evidence"] == "Must be a U.S. citizen."
    assert len(detail["history"]) == 1


def test_posting_detail_is_none_for_an_unknown_id(populated: Store) -> None:
    assert populated.posting(9999) is None


# --------------------------------------------------------------------------
# Against the real fixtures
# --------------------------------------------------------------------------

def test_a_real_posting_round_trips(store: Store, registry) -> None:
    from jobfilter.classifiers import RuleClassifier
    from jobfilter.cli import classify_url

    from .conftest import ASHBY_URL

    result = classify_url(ASHBY_URL, registry, RuleClassifier())
    store.record_run("test", [result])

    row = store.postings()[0]
    assert row["company_slug"] == "graymatter-robotics"
    assert row["label"] == "RESTRICTED_US_PERSON"
    assert row["title"] == "AI Research Intern: Foundation Models"
    assert "In order to comply" in row["evidence"]

    detail = store.posting(row["posting_id"])
    assert "Export Control Requirements" in detail["description_text"]
    # raw is kept so later phases need no re-fetch.
    raw = json.loads(
        store.connection.execute(
            "SELECT raw_json FROM postings WHERE id = ?", (row["posting_id"],)
        ).fetchone()["raw_json"]
    )
    assert raw["id"] == "dea0f08d-ebf5-4e55-b9cb-e6302014f429"


# --------------------------------------------------------------------------
# Board refresh bookkeeping (schema 4)
# --------------------------------------------------------------------------

def _refreshed(store: Store, *job_ids: str) -> tuple[int, dict[str, int]]:
    """Store postings at acme, then open a refresh run. -> run id, job id -> posting id."""
    store.record_run("t", [make_result(make_posting(job_id=j)) for j in job_ids])
    company_id = store.board_companies([Platform.LEVER])[0].company_id
    jobs = store.company_jobs(company_id)
    return store.start_run("refresh", 1), {j: jobs[j].posting_id for j in job_ids}


def test_board_companies_are_only_the_platforms_asked_for(store: Store) -> None:
    store.record_run("t", [
        make_result(make_posting(slug="zeta")),
        make_result(make_posting(slug="Alpha")),
        make_result(make_posting(slug="seen-on-linkedin", platform=Platform.LINKEDIN)),
    ])
    boards = store.board_companies([Platform.LEVER, Platform.ASHBY])
    assert [(b.platform, b.slug) for b in boards] == [
        (Platform.LEVER, "Alpha"), (Platform.LEVER, "zeta"),
    ]
    assert store.board_companies([]) == []


def test_company_jobs_carry_what_a_refresh_compares(store: Store) -> None:
    posting = make_posting(text="Must be a U.S. citizen.")
    store.record_run("t", [make_result(posting, label=Label.RESTRICTED_CITIZEN)])
    company_id = store.board_companies([Platform.LEVER])[0].company_id

    job = store.company_jobs(company_id)["j1"]
    assert job.url == posting.url
    assert job.content_hash == content_hash(posting.description_text)
    assert job.label == "RESTRICTED_CITIZEN"
    assert job.closed_at is None


def test_closing_keeps_the_posting_and_a_later_fetch_reopens_it(store: Store) -> None:
    run_id, ids = _refreshed(store, "j1", "j2")
    store.close_postings(run_id, [ids["j1"]])

    assert store.stats()["postings"] == 2  # closed, not deleted
    assert store.posting(ids["j1"])["closed_at"] is not None
    assert store.posting(ids["j2"])["closed_at"] is None

    store.upsert_posting(make_posting(job_id="j1"))
    assert store.posting(ids["j1"])["closed_at"] is None


def test_changes_show_for_the_latest_refresh_only(store: Store) -> None:
    run_id, ids = _refreshed(store, "j1", "j2")
    company_id = store.board_companies([Platform.LEVER])[0].company_id
    store.record_change(run_id, ids["j1"], Change.EDITED)
    store.record_change(run_id, ids["j1"], Change.RELABELED, "OPEN -> UNCLEAR")
    store.record_board_refresh(run_id, company_id, "ok", listed=2)

    assert store.posting(ids["j1"])["changes"] == "edited,relabeled"
    assert store.posting(ids["j2"])["changes"] is None
    assert store.count_postings(changed=True) == 1
    assert [p["job_id"] for p in store.postings(changed=True)] == ["j1"]

    # The next refresh finds nothing: nothing is flagged any more.
    later = store.start_run("refresh", 1)
    store.record_board_refresh(later, company_id, "ok", listed=2)
    assert store.posting(ids["j1"])["changes"] is None
    assert store.count_postings(changed=True) == 0


def test_company_summary_carries_the_latest_refresh(store: Store) -> None:
    run_id, ids = _refreshed(store, "j1", "j2", "j3")
    store.record_run("t", [make_result(make_posting(slug="never-refreshed"))])
    company_id = store.board_companies([Platform.LEVER])[0].company_id
    store.record_change(run_id, ids["j1"], Change.NEW)
    store.record_change(run_id, ids["j2"], Change.NEW)
    store.close_postings(run_id, [ids["j3"]])
    store.record_board_refresh(run_id, company_id, "ok", listed=2)

    rows = {row["company_slug"]: row for row in store.company_summary()}
    acme = rows["acme"]
    assert acme["closed"] == 1 and acme["total"] == 3
    assert acme["refresh"]["status"] == "ok" and acme["refresh"]["listed"] == 2
    assert acme["refresh"]["changes"] == {"new": 2, "closed": 1}
    assert acme["refresh"]["changed"] == 3
    assert rows["never-refreshed"]["refresh"] is None


def test_a_failed_refresh_is_summarized_with_its_error(store: Store) -> None:
    run_id, _ = _refreshed(store, "j1")
    company_id = store.board_companies([Platform.LEVER])[0].company_id
    store.record_board_refresh(run_id, company_id, "error", error="no Lever job board")

    refresh = store.company_summary()[0]["refresh"]
    assert refresh["status"] == "error"
    assert refresh["error"] == "no Lever job board"
    assert refresh["changed"] == 0


# --------------------------------------------------------------------------
# Apply-by dates
# --------------------------------------------------------------------------

def _days_from_today(days: int) -> str:
    from datetime import date, timedelta

    return (date.today() + timedelta(days=days)).strftime("%B %-d, %Y")


def _minimum(days: int) -> str:
    return f"Applications for this job will be accepted at least until {_days_from_today(days)}."


def _deadline(days: int) -> str:
    return f"Application deadline: {_days_from_today(days)}"


def test_upsert_stores_the_apply_by_date(store: Store) -> None:
    posting_id = store.upsert_posting(make_posting(text=f"Great job. {_minimum(3)}"))
    row = store.posting(posting_id)
    assert row["apply_by_firm"] == 0
    assert row["apply_by_text"] == _minimum(3)
    assert row["apply_by"] is not None


def test_a_refetch_rereads_the_apply_by_date(store: Store) -> None:
    store.upsert_posting(make_posting(text=_deadline(3)))
    posting_id = store.upsert_posting(make_posting(text="No date any more."))
    row = store.posting(posting_id)
    assert (row["apply_by"], row["apply_by_firm"], row["apply_by_text"]) == (None, None, None)


def test_a_v8_database_backfills_apply_by(tmp_path: Path) -> None:
    """Schema 9 added postings.apply_by*, read out of the stored text."""
    path = tmp_path / "v8.db"
    schema = Path("jobfilter/storage/schema.sql").read_text()
    for column in ("apply_by_firm", "apply_by_text", "apply_by"):
        schema = re.sub(rf"\n\s+(?:p\.)?{column}\b[^\n]*", "", schema)
    legacy = sqlite3.connect(path)
    legacy.executescript(schema)
    legacy.execute("PRAGMA user_version = 8")
    legacy.execute(
        "INSERT INTO companies (platform, slug, first_seen, last_seen)"
        " VALUES ('workday', 'nvidia', 'then', 'then')"
    )
    legacy.execute(
        "INSERT INTO postings (company_id, job_id, url, description_text,"
        " content_hash, first_seen, last_fetched)"
        " VALUES (1, 'JR2026819', 'https://old/url', ?, 'h',"
        " '2026-10-02T04:47:41+00:00', '2026-10-02T04:47:41+00:00')",
        ("Applications for this job will be accepted at least until October 4, 2026.",),
    )
    legacy.commit()
    legacy.close()

    with Store(path) as store:
        assert store.version == SCHEMA_VERSION
        row = store.connection.execute("SELECT * FROM posting_overview").fetchone()
        assert (row["apply_by"], row["apply_by_firm"]) == ("2026-10-04", 0)


@pytest.mark.parametrize(
    "text,closing",
    [
        pytest.param(_deadline(0), True, id="deadline-today"),
        pytest.param(_deadline(7), True, id="deadline-in-a-week"),
        pytest.param(_deadline(8), False, id="deadline-too-far"),
        pytest.param(_deadline(-1), False, id="firm-deadline-passed"),
        pytest.param(_minimum(-5), True, id="minimum-just-past"),
        pytest.param(_minimum(-8), False, id="minimum-long-past"),
        pytest.param("No date.", False, id="no-date"),
    ],
)
def test_closing_soon(store: Store, text: str, closing: bool) -> None:
    store.upsert_posting(make_posting(text=text))
    assert store.count_postings(closing=True) == int(closing)


def test_a_closed_posting_is_not_closing_soon(store: Store) -> None:
    posting_id = store.upsert_posting(make_posting(text=_deadline(2)))
    store.connection.execute("UPDATE postings SET closed_at = 'then' WHERE id = ?", (posting_id,))
    assert store.count_postings(closing=True) == 0


def test_deadline_order_puts_closing_soon_first_soonest_first(store: Store) -> None:
    store.upsert_posting(make_posting(job_id="later", text=_deadline(6)))
    store.upsert_posting(make_posting(job_id="sooner", text=_deadline(1)))
    store.upsert_posting(make_posting(job_id="far", text=_deadline(30)))
    store.upsert_posting(make_posting(job_id="none", text="No date."))
    newest = [p["job_id"] for p in store.postings()]
    assert newest == ["none", "far", "sooner", "later"]
    by_deadline = [p["job_id"] for p in store.postings(order="deadline")]
    assert by_deadline == ["sooner", "later", "none", "far"]


def test_a_v9_database_rereads_apply_by_with_the_workday_end_date(tmp_path: Path) -> None:
    """Schema 10 adds Workday's endDate as a fallback, for postings stored before it."""
    path = tmp_path / "v9.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(Path("jobfilter/storage/schema.sql").read_text())
    legacy.execute("PRAGMA user_version = 9")
    legacy.execute(
        "INSERT INTO companies (platform, slug, first_seen, last_seen)"
        " VALUES ('workday', 'cat', 'then', 'then')"
    )
    legacy.execute(
        "INSERT INTO postings (company_id, job_id, url, description_text, raw_json,"
        " content_hash, first_seen, last_fetched)"
        " VALUES (1, 'R1', 'https://old/url', 'No date.', '{\"endDate\": \"2026-10-11\"}',"
        " 'h', '2026-10-02T00:00:00+00:00', '2026-10-02T00:00:00+00:00')"
    )
    legacy.commit()
    legacy.close()

    with Store(path) as store:
        row = store.connection.execute("SELECT * FROM posting_overview").fetchone()
        assert (row["apply_by"], row["apply_by_firm"]) == ("2026-10-10", 1)


def test_upsert_falls_back_to_the_workday_end_date(store: Store) -> None:
    posting_id = store.upsert_posting(make_posting(text="No date.", raw={"endDate": "2026-10-11"}))
    assert store.posting(posting_id)["apply_by"] == "2026-10-10"


# --------------------------------------------------------------------------
# Posting dates
# --------------------------------------------------------------------------

def _days_ago(days: int) -> str:
    from datetime import date, timedelta

    return (date.today() - timedelta(days=days)).isoformat()


def test_upsert_stores_the_board_posting_date(store: Store) -> None:
    posting_id = store.upsert_posting(make_posting(raw={"createdAt": 1788288555846}))
    row = store.posting(posting_id)
    assert row["posted_on"] is not None and row["posted_on_approx"] == 0


def test_a_listing_date_survives_a_read_without_one(store: Store) -> None:
    """A later read that misses posted_ago keeps it, and so the date."""
    posting = dataclasses.replace(
        make_posting(platform=Platform.LINKEDIN, raw={}), posted_ago="2 weeks ago"
    )
    store.upsert_posting(posting)
    posting_id = store.upsert_posting(dataclasses.replace(posting, posted_ago=None))
    row = store.posting(posting_id)
    assert (row["posted_on"], row["posted_on_approx"]) == (_days_ago(14), 1)


def test_a_v10_database_backfills_posted_on(tmp_path: Path) -> None:
    """Schema 11 added postings.posted_on, read out of the stored raw JSON."""
    path = tmp_path / "v10.db"
    schema = Path("jobfilter/storage/schema.sql").read_text()
    for column in ("posted_on_approx", "posted_on"):
        schema = re.sub(rf"\n\s+(?:p\.)?{column}\b[^\n]*", "", schema)
    legacy = sqlite3.connect(path)
    legacy.executescript(schema)
    legacy.execute("PRAGMA user_version = 10")
    legacy.execute(
        "INSERT INTO companies (platform, slug, first_seen, last_seen)"
        " VALUES ('workday', 'nvidia', 'then', 'then')"
    )
    legacy.execute(
        "INSERT INTO postings (company_id, job_id, url, description_text, raw_json,"
        " content_hash, first_seen, last_fetched)"
        " VALUES (1, 'JR2026819', 'https://old/url', 'text', '{\"startDate\": \"2026-09-30\"}',"
        " 'h', '2026-10-02T00:00:00+00:00', '2026-10-02T00:00:00+00:00')"
    )
    legacy.commit()
    legacy.close()

    with Store(path) as store:
        assert store.version == SCHEMA_VERSION
        row = store.connection.execute("SELECT * FROM posting_overview").fetchone()
        assert (row["posted_on"], row["posted_on_approx"]) == ("2026-09-30", 0)


def _lever_posted(job_id: str, days: int) -> Posting:
    return make_posting(job_id=job_id, raw={"createdAt": f"{_days_ago(days)}T12:00:00"})


def test_posted_within_and_the_posted_order(store: Store) -> None:
    for job_id, days in (("old", 40), ("week", 6), ("new", 1)):
        store.upsert_posting(_lever_posted(job_id, days))
    store.upsert_posting(make_posting(job_id="undated", raw={}))

    assert store.count_postings(posted_within=7) == 2
    assert store.count_postings(posted_within=0) == 0
    assert [p["job_id"] for p in store.postings(order="posted")] == ["new", "week", "old", "undated"]
    assert [p["job_id"] for p in store.postings(order="posted_asc")] == ["old", "week", "new", "undated"]


# --------------------------------------------------------------------------
# Calendar: postings per day
# --------------------------------------------------------------------------

def _local_day(utc_iso: str) -> str:
    from datetime import datetime

    return datetime.fromisoformat(utc_iso).astimezone().date().isoformat()


def _calendar_store(store: Store) -> dict[str, int]:
    """Three postings first seen on two days; two applied on one, one maybe."""
    seen = {"a": "2026-10-04T18:00:00+00:00", "b": "2026-10-04T18:30:00+00:00", "c": "2026-10-06T18:00:00+00:00"}
    ids = {}
    for job_id, at in seen.items():
        ids[job_id] = store.upsert_posting(make_posting(job_id=job_id))
        store.connection.execute("UPDATE postings SET first_seen = ? WHERE id = ?", (at, ids[job_id]))
    for job_id, status in (("a", "applied"), ("c", "applied"), ("b", "maybe")):
        store.set_application(ids[job_id], status)
    store.connection.execute(
        "UPDATE applications SET updated_at = '2026-10-07T18:00:00+00:00'"
    )
    store.connection.commit()
    return ids


def test_day_counts_added_and_applied(store: Store) -> None:
    _calendar_store(store)
    first, second = _local_day("2026-10-04T18:00:00+00:00"), _local_day("2026-10-06T18:00:00+00:00")
    applied_day = _local_day("2026-10-07T18:00:00+00:00")

    assert store.day_counts("added", "2026-10-01", "2026-10-31") == {first: 2, second: 1}
    # A Maybe is not an application.
    assert store.day_counts("applied", "2026-10-01", "2026-10-31") == {applied_day: 2}
    assert store.day_counts("added", "2026-10-05", "2026-10-31") == {second: 1}, "range is inclusive, bounded"
    assert store.day_counts("added", "2026-10-01", "2026-10-31", app_status=["applied"]) == {first: 1, second: 1}


def test_a_days_postings(store: Store) -> None:
    _calendar_store(store)
    first = _local_day("2026-10-04T18:00:00+00:00")
    assert sorted(p["job_id"] for p in store.postings(added_on=first)) == ["a", "b"]
    applied_day = _local_day("2026-10-07T18:00:00+00:00")
    assert sorted(p["job_id"] for p in store.postings(applied_on=applied_day)) == ["a", "c"]
    assert store.count_postings(applied_on=first) == 0
