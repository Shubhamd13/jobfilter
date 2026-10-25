"""Table rendering. Pure functions, no network."""

from __future__ import annotations

import pytest

from jobfilter.models import Label, Platform, Result, Status
from jobfilter.report import (
    render_company_summary,
    render_results,
    render_table,
    truncate,
)


def make_result(
    *,
    company: str = "acme",
    title: str = "Engineer",
    label: Label = Label.OPEN,
    status: Status = Status.OK,
    evidence: str | None = None,
    error: str | None = None,
) -> Result:
    return Result(
        url=f"https://jobs.lever.co/{company}/1",
        status=status,
        platform=Platform.LEVER,
        company_slug=company if status is not Status.UNSUPPORTED else None,
        job_id="1",
        title=title if status is Status.OK else None,
        location="Remote",
        label=label,
        confidence=1.0 if status is Status.OK else None,
        evidence=evidence,
        matched_rule=None,
        matched_rules=(),
        classifier_name="rules" if status is Status.OK else None,
        error=error,
        error_type="JobNotFound" if error else None,
    )


# --------------------------------------------------------------------------
# truncate
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,width,expected",
    [
        pytest.param("short", 10, "short", id="fits"),
        pytest.param("exactly-10", 10, "exactly-10", id="exact"),
        pytest.param("far too long here", 10, "far too l…", id="cut"),
        pytest.param("a\nb\nc", 10, "a b c", id="newlines-flattened"),
        pytest.param("lots   of    space", 20, "lots of space", id="runs-collapsed"),
        pytest.param("anything", 0, "", id="zero-width"),
    ],
)
def test_truncate(text: str, width: int, expected: str) -> None:
    assert truncate(text, width) == expected


def test_truncate_never_exceeds_width() -> None:
    for width in range(1, 30):
        assert len(truncate("x" * 100, width)) == width


# --------------------------------------------------------------------------
# render_table
# --------------------------------------------------------------------------

def test_render_table_pads_and_rules() -> None:
    out = render_table(["A", "B"], [["1", "2"]], [3, 3]).splitlines()
    assert out == ["A    B", "---  ---", "1    2"]


def test_render_table_right_aligns() -> None:
    out = render_table(["N"], [["7"], ["100"]], [5], align="r").splitlines()
    assert out[2] == "    7"
    assert out[3] == "  100"


# --------------------------------------------------------------------------
# render_results
# --------------------------------------------------------------------------

def test_render_results_has_a_row_per_result() -> None:
    results = [make_result(title="A"), make_result(title="B"), make_result(title="C")]
    lines = render_results(results, width=100).splitlines()
    assert len(lines) == 2 + 3  # header + rule + rows


def test_render_results_is_empty_safe() -> None:
    assert render_results([], width=100) == "no URLs to check"


def test_render_results_respects_the_width_budget() -> None:
    results = [
        make_result(
            company="a-very-long-company-slug-indeed",
            title="An Extremely Long Job Title That Goes On",
            label=Label.RESTRICTED_US_PERSON,
            evidence="x" * 500,
        )
    ]
    for width in (60, 80, 100, 160):
        for line in render_results(results, width=width).splitlines():
            assert len(line) <= width, (width, len(line))


def test_render_results_shows_the_error_instead_of_evidence() -> None:
    result = make_result(status=Status.FETCH_ERROR, label=Label.UNCLEAR, error="gone")
    out = render_results([result], width=120)
    assert "[JobNotFound] gone" in out


def test_render_results_shows_a_dash_when_there_is_no_evidence() -> None:
    out = render_results([make_result()], width=100)
    assert out.splitlines()[2].rstrip().endswith("-")


def test_evidence_is_flattened_to_one_line() -> None:
    result = make_result(evidence="line one\nline two", label=Label.RESTRICTED_CITIZEN)
    out = render_results([result], width=140)
    assert len(out.splitlines()) == 3
    assert "line one line two" in out


# --------------------------------------------------------------------------
# render_company_summary
# --------------------------------------------------------------------------

def test_summary_counts_per_company() -> None:
    results = [
        make_result(company="acme", label=Label.RESTRICTED_CITIZEN),
        make_result(company="acme", label=Label.OPEN),
        make_result(company="beta", label=Label.OPEN),
    ]
    lines = render_company_summary(results, width=100).splitlines()
    acme = next(line for line in lines if line.startswith("acme"))
    assert acme.split() == ["acme", "2", "1", "0", "0", "1", "0"]


def test_summary_counts_every_restriction_label_as_restricted() -> None:
    results = [
        make_result(label=Label.RESTRICTED_CITIZEN),
        make_result(label=Label.RESTRICTED_US_PERSON),
        make_result(label=Label.RESTRICTED_CLEARANCE),
        make_result(label=Label.INFO_NO_SPONSORSHIP),
    ]
    row = render_company_summary(results, width=100).splitlines()[2]
    assert row.split() == ["acme", "4", "3", "0", "1", "0", "0"]


def test_summary_counts_failures_as_errors_not_as_unclear() -> None:
    """A failed fetch is labelled UNCLEAR, but it is not a classification."""
    results = [
        make_result(label=Label.UNCLEAR),
        make_result(status=Status.FETCH_ERROR, label=Label.UNCLEAR, error="boom"),
    ]
    row = render_company_summary(results, width=100).splitlines()[2]
    assert row.split() == ["acme", "2", "0", "1", "0", "0", "1"]


def test_summary_puts_the_most_restricted_company_first() -> None:
    results = [
        make_result(company="clean", label=Label.OPEN),
        make_result(company="blocked", label=Label.RESTRICTED_CITIZEN),
    ]
    lines = render_company_summary(results, width=100).splitlines()
    assert lines[2].startswith("blocked")
    assert lines[3].startswith("clean")


def test_summary_totals_row_appears_only_for_several_companies() -> None:
    one = render_company_summary([make_result()], width=100)
    assert "ALL" not in one

    two = render_company_summary(
        [make_result(company="a"), make_result(company="b")], width=100
    )
    all_row = two.splitlines()[-1]
    assert all_row.split() == ["ALL", "2", "0", "0", "0", "2", "0"]


def test_unparsed_urls_group_under_a_placeholder_company() -> None:
    result = make_result(status=Status.UNSUPPORTED, label=Label.UNCLEAR, error="nope")
    assert "(unparsed)" in render_company_summary([result], width=100)
