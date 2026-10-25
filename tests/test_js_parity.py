"""The Python and JavaScript rule engines must agree, case for case.

`rules.json` is shared data, so the patterns cannot drift. The pipeline is
implemented twice, so it can -- this file is what stops it. Every case lives in
tests/cases.json; add new ones there rather than to one language's tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobfilter.classifiers import RuleClassifier, resolve_rules_path

from .jsrunner import find_chrome, run_js

CLASSIFIER_JS = Path("extension/src/classifier.js")
CASES_PATH = Path("tests/cases.json")

pytestmark = pytest.mark.skipif(
    find_chrome() is None,
    reason="needs Chrome/Chromium to run the JavaScript engine",
)


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text())["cases"]


@pytest.fixture(scope="module")
def rules_doc() -> dict:
    return json.loads(resolve_rules_path().read_text())


@pytest.fixture(scope="module")
def js_results(rules_doc: dict) -> dict[str, dict]:
    """Classify every case in V8, once, and index by case id."""
    cases = load_cases()
    values = run_js(
        [CLASSIFIER_JS],
        {"RULES": rules_doc, "CASES": cases},
        """
        const clf = JobFilterRules.RuleClassifier(RULES);
        const out = {};
        for (const c of CASES) out[c.id] = clf.classify(c.text);
        return out;
        """,
    )
    return values


@pytest.fixture(scope="module")
def py_results() -> dict[str, dict]:
    clf = RuleClassifier()
    out = {}
    for case in load_cases():
        decision = clf.classify_text(case["text"])
        out[case["id"]] = {
            "label": str(decision.label),
            "evidence": decision.evidence,
            "matchedRule": decision.matched_rule,
            "matchedRules": list(decision.matched_rules),
            "confidence": decision.confidence,
        }
    return out


# --------------------------------------------------------------------------
# The JavaScript engine against the corpus
# (the Python side runs in test_rule_classifier.py, which needs no browser)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_javascript_matches_the_corpus(case: dict, js_results: dict) -> None:
    got = js_results[case["id"]]
    assert got["label"] == case["label"], case["text"]
    if "rule" in case:
        assert got["matchedRule"] == case["rule"]


# --------------------------------------------------------------------------
# And against each other -- the part that catches drift
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_the_two_engines_agree(
    case: dict, py_results: dict, js_results: dict
) -> None:
    python, javascript = py_results[case["id"]], js_results[case["id"]]
    assert javascript["label"] == python["label"]
    assert javascript["evidence"] == python["evidence"]
    assert javascript["matchedRule"] == python["matchedRule"]
    assert javascript["matchedRules"] == python["matchedRules"]
    assert javascript["confidence"] == python["confidence"]


def test_the_engines_agree_on_the_real_fixture_text(js_results, rules_doc) -> None:
    """Not a synthetic string: the whole GrayMatter description."""
    from .conftest import FIXTURES

    board = json.loads(
        (FIXTURES / "ashby_board_graymatter-robotics.json").read_text()
    )
    texts = {job["id"]: job.get("descriptionPlain") or "" for job in board["jobs"]}

    javascript = run_js(
        [CLASSIFIER_JS],
        {"RULES": rules_doc, "TEXTS": texts},
        """
        const clf = JobFilterRules.RuleClassifier(RULES);
        const out = {};
        for (const [id, text] of Object.entries(TEXTS)) out[id] = clf.classify(text);
        return out;
        """,
    )

    clf = RuleClassifier()
    for job_id, text in texts.items():
        python = clf.classify_text(text)
        assert javascript[job_id]["label"] == str(python.label), job_id
        assert javascript[job_id]["evidence"] == python.evidence, job_id
        assert javascript[job_id]["matchedRules"] == list(python.matched_rules), job_id


# --------------------------------------------------------------------------
# Sentence splitting, the part most likely to diverge between engines
# --------------------------------------------------------------------------

SPLIT_CASES = [
    "Applicants must be a U.S. citizen to apply.",
    "Refugee under 8 U.S.C. 1157 qualifies here.",
    "A resident (i.e. green card holder) may apply.",
    "We value experience. You will build robots.",
    "One. Two. Three.",
    "Head\n\n- one\n- two",
    'He said "no." She agreed.',
    "version 2.5 of the stack",
    "1. Design systems",
    "Acme Inc. requires U.S. citizenship.",
]


def test_sentence_splitting_is_identical(rules_doc: dict) -> None:
    from jobfilter.classifiers import RuleSet
    from jobfilter.classifiers.sentences import split_sentences

    javascript = run_js(
        [CLASSIFIER_JS],
        {"RULES": rules_doc, "TEXTS": SPLIT_CASES},
        """
        const rs = JobFilterRules.buildRuleSet(RULES);
        return TEXTS.map((t) => JobFilterRules.splitSentences(t, rs.protect));
        """,
    )

    protect = RuleSet.load().protect
    for text, js_split in zip(SPLIT_CASES, javascript):
        assert js_split == split_sentences(text, protect), text


# --------------------------------------------------------------------------
# The extension's copy of rules.json must not drift from the canonical one
# --------------------------------------------------------------------------

def test_the_extension_ships_the_canonical_rules() -> None:
    """A packed extension needs its own copy; `make sync-rules` makes it.

    Byte-identical, not merely equivalent -- if the two ever diverge the whole
    'one shared rules file' claim is false.
    """
    canonical = resolve_rules_path().read_bytes()
    shipped = Path("extension/rules.json").read_bytes()
    assert shipped == canonical, "run `make sync-rules`"
