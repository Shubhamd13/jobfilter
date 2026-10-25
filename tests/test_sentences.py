"""Sentence splitting. Over-splitting is the dangerous failure, not under-splitting."""

from __future__ import annotations

import pytest

from jobfilter.classifiers import RuleSet
from jobfilter.classifiers.sentences import split_sentences


@pytest.fixture(scope="module")
def protect() -> list:
    return RuleSet.load().protect


def split(text: str, protect: list) -> list[str]:
    return split_sentences(text, protect)


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("One. Two.", ["One.", "Two."], id="basic"),
        pytest.param("One! Two? Three.", ["One!", "Two?", "Three."], id="all-terminators"),
        pytest.param("Only one sentence", ["Only one sentence"], id="no-terminator"),
        pytest.param("", [], id="empty"),
        pytest.param("   \n\n  ", [], id="whitespace-only"),
        pytest.param("A\nB\nC", ["A", "B", "C"], id="newlines-are-boundaries"),
        pytest.param("Head\n\n- one\n- two", ["Head", "- one", "- two"], id="bullets-separate"),
    ],
)
def test_basic_splitting(text: str, expected: list[str], protect: list) -> None:
    assert split(text, protect) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Applicants must be a U.S. citizen to apply.", id="us-citizen"),
        pytest.param("Refugee under 8 U.S.C. 1157 qualifies here.", id="usc-statute"),
        pytest.param("A resident (i.e. green card holder) may apply.", id="ie"),
        pytest.param("Some roles (e.g. defense work) are restricted.", id="eg"),
        pytest.param("A Ph.D. in robotics is required for this role.", id="phd"),
        pytest.param("Acme Inc. requires U.S. citizenship for this job.", id="inc"),
        pytest.param("See No. 5 below for the citizenship policy here.", id="no-abbrev"),
    ],
)
def test_abbreviations_never_split(text: str, protect: list) -> None:
    """Splitting `U.S.` from `citizen` would destroy the match we exist to make."""
    assert split(text, protect) == [text]


def test_the_ground_truth_sentence_survives_intact(protect: list) -> None:
    """Three separate abbreviation traps in one real sentence."""
    text = (
        "In order to comply with the export regulations of the United States "
        "Government, it is required that the applicant must fall under one of "
        "the following categories: (i) U.S. citizen or national, (ii) U.S. "
        "lawful permanent resident (i.e. current green card holder), (iii) "
        "Refugee under 8 U.S.C. 1157, or (iv) Asylee under 8 U.S.C. 1158."
    )
    assert split(text, protect) == [text]


def test_ordinary_sentences_ending_in_a_letter_still_split(protect: list) -> None:
    """The abbreviation rule must not swallow every normal sentence end."""
    assert split("We value experience. You will build robots.", protect) == [
        "We value experience.",
        "You will build robots.",
    ]


def test_lowercase_after_a_period_is_not_a_boundary(protect: list) -> None:
    assert split("version 2.5 of the stack", protect) == ["version 2.5 of the stack"]


def test_numbered_list_markers_do_not_split(protect: list) -> None:
    assert split("1. Design systems", protect) == ["1. Design systems"]


def test_quotes_stay_with_their_sentence(protect: list) -> None:
    assert split('He said "no." She agreed.', protect) == ['He said "no."', "She agreed."]
