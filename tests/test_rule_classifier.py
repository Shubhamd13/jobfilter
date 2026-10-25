"""RuleClassifier. Synthetic strings per label, every gotcha, plus ground truth."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobfilter.classifiers import RuleClassifier, RuleSet, resolve_rules_path
from jobfilter.models import Label

from .conftest import ASHBY_URL, GH_URL, LEVER_URL


@pytest.fixture(scope="module")
def clf() -> RuleClassifier:
    return RuleClassifier()


def label(clf: RuleClassifier, text: str) -> Label:
    return clf.classify_text(text).label


# --------------------------------------------------------------------------
# The shared corpus: every text -> label case lives in tests/cases.json so the
# JavaScript engine runs exactly the same set. Add cases there, not here.
# This file keeps the checks that are about structure rather than labels.
# --------------------------------------------------------------------------

def load_cases() -> list[dict]:
    return json.loads(Path("tests/cases.json").read_text())["cases"]


def test_the_corpus_is_well_formed() -> None:
    cases = load_cases()
    ids = [c["id"] for c in cases]
    labels = {label.value for label in Label}

    assert len(ids) == len(set(ids)), "case ids must be unique"
    assert len(cases) >= 50, "the corpus is the contract; keep it substantial"
    for case in cases:
        assert case["label"] in labels, case["id"]
        assert set(case) <= {"id", "text", "label", "rule"}, case["id"]


def test_every_label_is_represented() -> None:
    assert {c["label"] for c in load_cases()} == {label.value for label in Label}


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_python_matches_the_corpus(clf: RuleClassifier, case: dict) -> None:
    """Covers every label, both gotchas and the hedging cases."""
    decision = clf.classify_text(case["text"])
    assert str(decision.label) == case["label"], case["text"]
    if "rule" in case:
        assert decision.matched_rule == case["rule"]


# --------------------------------------------------------------------------
# Decision shape and evidence
# --------------------------------------------------------------------------

def test_evidence_drops_the_rendered_bullet_marker(clf: RuleClassifier) -> None:
    """`- ` comes from our HTML rendering, not from the posting's own words."""
    decision = clf.classify_text("- Must be a U.S. citizen.")
    assert decision.label is Label.RESTRICTED_CITIZEN
    assert decision.evidence == "Must be a U.S. citizen."

def test_evidence_is_the_triggering_sentence_only(clf: RuleClassifier) -> None:
    text = (
        "We build robots in Los Angeles. This position requires U.S. "
        "citizenship. Benefits include equity."
    )
    decision = clf.classify_text(text)
    assert decision.evidence == "This position requires U.S. citizenship."


def test_evidence_prefers_the_stated_restriction_over_a_heading(
    clf: RuleClassifier,
) -> None:
    """A heading and a full sentence can score the same label; quote the sentence."""
    text = (
        "Export Control Requirements\n\n"
        "The applicant must be a U.S. citizen or lawful permanent resident."
    )
    decision = clf.classify_text(text)
    assert decision.evidence is not None
    assert decision.evidence.startswith("The applicant must be")


def test_open_decision_has_no_evidence(clf: RuleClassifier) -> None:
    decision = clf.classify_text("We are hiring a robotics engineer.")
    assert decision.label is Label.OPEN
    assert decision.evidence is None
    assert decision.matched_rule is None
    assert decision.matched_rules == ()


def test_decision_metadata(clf: RuleClassifier) -> None:
    decision = clf.classify_text("Must be a U.S. citizen.")
    assert decision.confidence == 1.0
    assert decision.classifier_name == "rules"
    assert decision.matched_rule == "must-be-us-citizen"
    assert decision.matched_rule == decision.matched_rules[0]


def test_label_is_restriction_flag() -> None:
    assert Label.RESTRICTED_CITIZEN.is_restriction
    assert Label.RESTRICTED_US_PERSON.is_restriction
    assert Label.RESTRICTED_CLEARANCE.is_restriction
    assert not Label.INFO_NO_SPONSORSHIP.is_restriction
    assert not Label.UNCLEAR.is_restriction
    assert not Label.OPEN.is_restriction


# --------------------------------------------------------------------------
# Ground truth, offline against the saved fixtures
# --------------------------------------------------------------------------

def test_graymatter_ground_truth(registry, clf: RuleClassifier) -> None:
    """CLAUDE.md's named case: Ashby graymatter-robotics -> RESTRICTED_US_PERSON."""
    from jobfilter.url_parser import parse_url

    posting = registry.fetch(parse_url(ASHBY_URL))
    decision = clf.classify(posting)

    assert decision.label is Label.RESTRICTED_US_PERSON
    assert decision.confidence == 1.0
    assert decision.evidence is not None
    assert "U.S. citizen or national" in decision.evidence
    assert "lawful permanent resident" in decision.evidence
    assert "Refugee under 8 U.S.C." in decision.evidence
    assert "Asylee under 8 U.S.C." in decision.evidence
    # The whole sentence, not a fragment chopped at "U.S."
    assert decision.evidence.startswith("In order to comply")
    assert decision.evidence.endswith("1158.")


@pytest.mark.parametrize("url", [GH_URL, LEVER_URL])
def test_the_other_two_fixtures_are_open(registry, clf: RuleClassifier, url: str) -> None:
    from jobfilter.url_parser import parse_url

    decision = clf.classify(registry.fetch(parse_url(url)))
    assert decision.label is Label.OPEN, decision.evidence


# --------------------------------------------------------------------------
# rules.json integrity
# --------------------------------------------------------------------------

def test_rules_json_is_self_consistent() -> None:
    doc = json.loads(resolve_rules_path().read_text())

    ids = [
        item["id"]
        for key in ("exclusions", "negations", "rules", "open_signals")
        for item in doc[key]
    ]
    assert len(ids) == len(set(ids)), "rule ids must be unique"

    labels = {label.value for label in Label}
    assert set(doc["severity"]) == labels, "severity must cover every label"
    for rule in doc["rules"]:
        assert rule["label"] in labels
    for signal in doc["negations"]:
        assert set(signal["applies_to"]) <= labels
    for signal in doc["open_signals"]:
        assert set(signal["conflicts_with"]) <= labels


def test_suppresses_rules_reference_real_rules() -> None:
    doc = json.loads(resolve_rules_path().read_text())
    rule_ids = {rule["id"] for rule in doc["rules"]}
    for signal in doc["negations"]:
        unknown = set(signal.get("suppresses_rules", ())) - rule_ids
        assert not unknown, f"{signal['id']} suppresses unknown rules: {unknown}"


def test_unless_guard_blocks_a_negated_open_signal(clf: RuleClassifier) -> None:
    """'no sponsorship is offered' must not read as 'sponsorship is offered'."""
    assert label(clf, "No visa sponsorship is offered.") is Label.INFO_NO_SPONSORSHIP
    assert label(clf, "Visa sponsorship is offered.") is Label.OPEN


def test_negation_cancels_the_bare_mention_rule_too(clf: RuleClassifier) -> None:
    """Otherwise a negated sentence still leaks an UNCLEAR from `clearance-mention`."""
    decision = clf.classify_text("No security clearance required.")
    assert decision.label is Label.OPEN
    assert decision.matched_rules == ()


def test_every_pattern_has_a_note() -> None:
    """The notes are the documentation a future JS port will read."""
    doc = json.loads(resolve_rules_path().read_text())
    for key in ("exclusions", "negations", "rules", "open_signals"):
        for item in doc[key]:
            assert item.get("note"), f"{item['id']} has no note"


def test_ruleset_can_be_loaded_from_an_explicit_path(tmp_path) -> None:
    doc = json.loads(resolve_rules_path().read_text())
    doc["rules"] = [
        {"id": "only-rule", "label": "RESTRICTED_CITIZEN", "flags": "i",
         "pattern": r"\bzebra\b", "note": "test"}
    ]
    custom = tmp_path / "rules.json"
    custom.write_text(json.dumps(doc))

    clf = RuleClassifier(RuleSet.load(custom))
    assert clf.classify_text("We saw a zebra.").label is Label.RESTRICTED_CITIZEN
    assert clf.classify_text("Must be a U.S. citizen.").label is Label.OPEN
