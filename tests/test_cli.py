"""CLI end to end. Offline: the registry is fed the MockTransport from conftest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobfilter.cli import (
    build_parser,
    classify_url,
    read_sources,
    report,
    run,
    write_jsonl,
)
from jobfilter.classifiers import RuleClassifier
from jobfilter.models import Label, Platform, Status

from .conftest import ASHBY_SLUG, ASHBY_URL, GH_URL, LEVER_URL, ashby_sibling_job_id

BAD_URL = "https://www.linkedin.com/jobs/view/12345"
MISSING_JOB_URL = f"https://jobs.ashbyhq.com/{ASHBY_SLUG}/00000000-0000-0000-0000-000000000000"
MISSING_BOARD_URL = "https://jobs.ashbyhq.com/no-such-company/abc"


@pytest.fixture(scope="module")
def clf() -> RuleClassifier:
    return RuleClassifier()


# --------------------------------------------------------------------------
# Reading URLs from files and args
# --------------------------------------------------------------------------

def test_read_sources_accepts_bare_urls() -> None:
    assert read_sources([ASHBY_URL, GH_URL]) == [ASHBY_URL, GH_URL]


def test_read_sources_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "urls.txt"
    path.write_text(f"# a comment\n\n{ASHBY_URL}\n  {GH_URL}  \n\n# trailing\n")
    assert read_sources([str(path)]) == [ASHBY_URL, GH_URL]


def test_read_sources_mixes_files_and_urls(tmp_path: Path) -> None:
    path = tmp_path / "urls.txt"
    path.write_text(ASHBY_URL + "\n")
    assert read_sources([str(path), LEVER_URL]) == [ASHBY_URL, LEVER_URL]


def test_read_sources_keeps_duplicates() -> None:
    """Deduping is the caller's call; the Ashby cache makes repeats cheap."""
    assert read_sources([ASHBY_URL, ASHBY_URL]) == [ASHBY_URL, ASHBY_URL]


def test_read_sources_ignores_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "urls.txt"
    path.write_text("\n\n   \n")
    assert read_sources([str(path)]) == []


def test_the_shipped_urls_file_parses() -> None:
    urls = read_sources(["urls.txt"])
    assert len(urls) == 3
    assert all(u.startswith("https://") for u in urls)


# --------------------------------------------------------------------------
# classify_url: one Result per URL, whatever happens
# --------------------------------------------------------------------------

def test_successful_url_becomes_an_ok_result(registry, clf: RuleClassifier) -> None:
    result = classify_url(ASHBY_URL, registry, clf)
    assert result.status is Status.OK
    assert result.label is Label.RESTRICTED_US_PERSON
    assert result.company_slug == ASHBY_SLUG
    assert result.title == "AI Research Intern: Foundation Models"
    assert result.confidence == 1.0
    assert result.classifier_name == "rules"
    assert result.error is None


def test_unsupported_url_still_produces_a_row(registry, clf: RuleClassifier) -> None:
    result = classify_url(BAD_URL, registry, clf)
    assert result.status is Status.UNSUPPORTED
    assert result.label is Label.UNCLEAR
    assert result.platform is Platform.UNSUPPORTED
    assert result.company_slug is None
    assert result.error


@pytest.mark.parametrize(
    "url,error_type",
    [
        pytest.param(MISSING_JOB_URL, "JobNotFound", id="missing-job"),
        pytest.param(MISSING_BOARD_URL, "BoardNotFound", id="missing-board"),
    ],
)
def test_fetch_failures_still_produce_a_row(
    registry, clf: RuleClassifier, url: str, error_type: str
) -> None:
    result = classify_url(url, registry, clf)
    assert result.status is Status.FETCH_ERROR
    assert result.label is Label.UNCLEAR
    assert result.error_type == error_type
    assert result.confidence is None


def test_an_unexpected_exception_does_not_end_the_run(clf: RuleClassifier) -> None:
    class Exploding:
        def fetch(self, parsed):
            raise RuntimeError("kaboom")

    result = classify_url(ASHBY_URL, Exploding(), clf)
    assert result.status is Status.FETCH_ERROR
    assert result.error_type == "RuntimeError"
    assert "kaboom" in (result.error or "")


# --------------------------------------------------------------------------
# run: nothing is ever dropped
# --------------------------------------------------------------------------

def test_run_returns_one_result_per_url_in_order(registry, clf: RuleClassifier) -> None:
    urls = [ASHBY_URL, BAD_URL, GH_URL, MISSING_JOB_URL, LEVER_URL]
    results = run(urls, registry, clf)
    assert [r.url for r in results] == urls


def test_run_reuses_the_ashby_board_cache(registry, transport, clf: RuleClassifier) -> None:
    urls = [ASHBY_URL, f"https://jobs.ashbyhq.com/{ASHBY_SLUG}/{ashby_sibling_job_id()}"]
    results = run(urls, registry, clf)
    assert len(results) == 2
    assert len(transport.requests) == 1


# --------------------------------------------------------------------------
# JSONL
# --------------------------------------------------------------------------

def test_write_jsonl_round_trips(registry, clf: RuleClassifier, tmp_path: Path) -> None:
    results = run([ASHBY_URL, BAD_URL, LEVER_URL], registry, clf)
    out = tmp_path / "nested" / "results.jsonl"
    write_jsonl(results, out)

    lines = out.read_text().splitlines()
    assert len(lines) == 3

    rows = [json.loads(line) for line in lines]
    assert rows[0]["label"] == "RESTRICTED_US_PERSON"
    assert rows[0]["evidence"].startswith("In order to comply")
    assert rows[0]["matched_rules"][0] == "lawful-permanent-resident"
    assert rows[1]["status"] == "unsupported"
    assert rows[2]["label"] == "OPEN"


def test_jsonl_rows_are_flat_and_serializable(registry, clf: RuleClassifier, tmp_path: Path) -> None:
    results = run([ASHBY_URL], registry, clf)
    write_jsonl(results, tmp_path / "r.jsonl")
    row = json.loads((tmp_path / "r.jsonl").read_text().splitlines()[0])
    assert set(row) == {
        "url", "status", "origin", "platform", "company_slug", "job_id", "title",
        "location", "label", "confidence", "evidence", "matched_rule",
        "matched_rules", "classifier_name", "error", "error_type",
    }
    assert all(not isinstance(v, (dict, tuple)) for v in row.values())


# --------------------------------------------------------------------------
# Printed output
# --------------------------------------------------------------------------

def test_report_prints_both_tables(registry, clf: RuleClassifier, capsys) -> None:
    import sys

    results = run([ASHBY_URL, GH_URL, LEVER_URL], registry, clf)
    report(results, sys.stdout, width=120)
    out = capsys.readouterr().out

    assert "COMPANY" in out and "EVIDENCE" in out
    assert "RESTRICT" in out and "ERRORS" in out
    assert "RESTRICTED_US_PERSON" in out
    assert "graymatter-robotics" in out
    assert "ALL" in out


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def test_parser_defaults() -> None:
    args = build_parser().parse_args(["check", "urls.txt"])
    assert args.command == "check"
    assert args.sources == ["urls.txt"]
    assert args.out is None
    assert args.timeout == 10.0
    assert args.quiet is False
    assert args.db is None
    assert args.no_db is False


def test_main_records_to_the_database_by_default(tmp_path: Path, capsys) -> None:
    from jobfilter.cli import main

    db = tmp_path / "jobfilter.db"
    main(["check", BAD_URL, "--db", str(db), "--quiet"])

    from jobfilter.storage import Store

    with Store(db) as store:
        assert store.stats()["runs"] == 1
        assert store.stats()["errors"] == 1


def test_no_db_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    from jobfilter.cli import main
    from jobfilter.storage.store import DB_ENV_VAR

    db = tmp_path / "should-not-exist.db"
    monkeypatch.setenv(DB_ENV_VAR, str(db))
    main(["check", BAD_URL, "--quiet", "--no-db"])
    assert not db.exists()


def test_parser_accepts_the_documented_invocation() -> None:
    args = build_parser().parse_args(
        ["check", "urls.txt", "--out", "results.jsonl"]
    )
    assert args.sources == ["urls.txt"]
    assert str(args.out) == "results.jsonl"


def test_parser_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_parser_requires_a_source() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["check"])


# --------------------------------------------------------------------------
# main(): exit codes. Unsupported URLs short-circuit before any request, so
# these stay offline without needing the mock transport.
# --------------------------------------------------------------------------

def test_main_exits_1_when_a_url_fails(capsys) -> None:
    from jobfilter.cli import main

    assert main(["check", BAD_URL, "--width", "100", "--no-db"]) == 1
    out = capsys.readouterr().out
    assert "(unparsed)" in out
    assert "UNCLEAR" in out


def test_main_exits_2_when_no_urls_are_found(tmp_path: Path, capsys) -> None:
    from jobfilter.cli import main

    empty = tmp_path / "empty.txt"
    empty.write_text("# only comments\n\n")
    assert main(["check", str(empty)]) == 2
    assert "no URLs found" in capsys.readouterr().err


def test_main_quiet_prints_nothing_but_still_writes(tmp_path: Path, capsys) -> None:
    from jobfilter.cli import main

    out = tmp_path / "r.jsonl"
    main(["check", BAD_URL, "--quiet", "--out", str(out), "--no-db"])
    assert capsys.readouterr().out == ""
    assert len(out.read_text().splitlines()) == 1


@pytest.mark.live
def test_main_end_to_end_against_the_real_apis(tmp_path: Path, capsys) -> None:
    """The exact invocation from CLAUDE.md, over the network."""
    from jobfilter.cli import main

    out = tmp_path / "results.jsonl"
    db = tmp_path / "jobfilter.db"
    code = main(
        ["check", "urls.txt", "--out", str(out), "--db", str(db), "--width", "120"]
    )
    assert code == 0

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 3
    labels = {row["company_slug"]: row["label"] for row in rows}
    assert labels["graymatter-robotics"] == "RESTRICTED_US_PERSON"
    assert labels["undercontrolroboticsinc"] == "OPEN"
    assert labels["rai"] == "OPEN"

    printed = capsys.readouterr().out
    assert "RESTRICTED_US_PERSON" in printed
    assert f"wrote 3 result(s) to {out}" in printed
    assert f"recorded 3 result(s) in {db}" in printed

    from jobfilter.storage import Store

    with Store(db) as store:
        assert store.stats()["postings"] == 3
        assert store.stats()["restricted"] == 1


# --------------------------------------------------------------------------
# reclassify: new rules over stored postings, no fetching
# --------------------------------------------------------------------------

def _stored(tmp_path: Path) -> Path:
    """A database holding verdicts an older, wrong rule set gave."""
    from jobfilter.storage import Store

    from .test_storage import make_posting, make_result

    db = tmp_path / "jobs.db"
    with Store(db) as store:
        store.record_run(
            "t",
            [
                # Was OPEN; the current rules say citizen-only.
                make_result(
                    make_posting(job_id="a", text="U.S. Citizen Required"),
                    label=Label.OPEN,
                ),
                # Was a clearance bar; 'Public Trust teams' is a department.
                make_result(
                    make_posting(
                        job_id="b",
                        text="Collaborate with Legal and Public Trust teams.",
                    ),
                    label=Label.RESTRICTED_CLEARANCE,
                    evidence="Collaborate with Legal and Public Trust teams.",
                    matched_rule="named-clearance",
                ),
                # Already right.
                make_result(
                    make_posting(job_id="c", text="We are hiring."), label=Label.OPEN
                ),
            ],
        )
    return db


def _labels(db: Path) -> dict[str, str]:
    from jobfilter.storage import Store

    with Store(db) as store:
        return {p["job_id"]: p["label"] for p in store.postings()}


def test_reclassify_updates_stored_labels(tmp_path: Path, capsys) -> None:
    from jobfilter.cli import main

    db = _stored(tmp_path)
    assert main(["reclassify", "--db", str(db)]) == 0

    assert _labels(db) == {"a": "RESTRICTED_CITIZEN", "b": "OPEN", "c": "OPEN"}
    out = capsys.readouterr().out
    assert "3 postings reclassified; 2 changed label." in out
    assert "OPEN" in out and "-> RESTRICTED_CITIZEN" in out


def test_reclassify_keeps_the_old_verdict_in_history(tmp_path: Path) -> None:
    from jobfilter.cli import main
    from jobfilter.storage import Store

    db = _stored(tmp_path)
    main(["reclassify", "--db", str(db)])
    with Store(db) as store:
        posting_id = next(p["posting_id"] for p in store.postings() if p["job_id"] == "b")
        history = [h["label"] for h in store.posting(posting_id)["history"]]
    assert history == ["RESTRICTED_CLEARANCE", "OPEN"]


def test_reclassify_twice_changes_nothing_the_second_time(tmp_path: Path, capsys) -> None:
    from jobfilter.cli import main

    db = _stored(tmp_path)
    main(["reclassify", "--db", str(db)])
    capsys.readouterr()
    main(["reclassify", "--db", str(db)])
    assert "0 changed label." in capsys.readouterr().out


def test_reclassify_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    from jobfilter.cli import main

    db = _stored(tmp_path)
    before = _labels(db)
    main(["reclassify", "--db", str(db), "--dry-run"])
    assert _labels(db) == before
    assert "2 would change label." in capsys.readouterr().out
