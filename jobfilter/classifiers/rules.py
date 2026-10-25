"""Regex rule classifier. Every pattern lives in rules.json, none in here.

The pipeline, per posting:

  1. split the description into sentences (evidence has to be quotable)
  2. skip EEO boilerplate sentences entirely
  3. match rules per sentence, dropping any label negated in that same sentence
  4. within a sentence, let US_PERSON supersede CITIZEN
  5. across sentences, keep the most restrictive label by severity
  6. if an open signal contradicts the winner anywhere in the posting, UNCLEAR
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..models import Decision, Label, Posting
from .sentences import split_sentences

RULES_FILENAME = "rules.json"
RULES_ENV_VAR = "JOBFILTER_RULES"
CLASSIFIER_NAME = "rules"


def _compile(spec: dict[str, Any], default_flags: str) -> re.Pattern[str]:
    flags = spec.get("flags", default_flags)
    return re.compile(spec["pattern"], re.IGNORECASE if "i" in flags else 0)


def _compile_unless(spec: dict[str, Any]) -> re.Pattern[str] | None:
    """A pattern that cancels this one when it matches the same sentence."""
    if "unless" not in spec:
        return None
    flags = spec.get("unless_flags", "i")
    return re.compile(spec["unless"], re.IGNORECASE if "i" in flags else 0)


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    label: Label
    pattern: re.Pattern[str]
    note: str
    unless: re.Pattern[str] | None = None

    def matches(self, sentence: str) -> bool:
        return bool(self.pattern.search(sentence)) and not (
            self.unless is not None and self.unless.search(sentence)
        )


@dataclass(frozen=True, slots=True)
class Signal:
    """A negation or an open signal: a pattern plus what it acts on."""

    id: str
    pattern: re.Pattern[str]
    labels: frozenset[Label]
    note: str
    # Rule ids this signal cancels outright, on top of its labels.
    suppresses_rules: frozenset[str] = frozenset()
    unless: re.Pattern[str] | None = None

    def matches(self, sentence: str) -> bool:
        return bool(self.pattern.search(sentence)) and not (
            self.unless is not None and self.unless.search(sentence)
        )


@dataclass(frozen=True, slots=True)
class RuleSet:
    version: int
    severity: dict[Label, int]
    supersedes: dict[Label, frozenset[Label]]
    protect: list[re.Pattern[str]]
    exclusions: list[Signal]
    negations: list[Signal]
    rules: list[Rule]
    open_signals: list[Signal]

    @classmethod
    def load(cls, path: str | Path | None = None) -> RuleSet:
        return cls.from_dict(json.loads(resolve_rules_path(path).read_text()))

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> RuleSet:
        default_flags = doc.get("default_flags", "i")

        def signals(key: str, labels_key: str) -> list[Signal]:
            return [
                Signal(
                    id=spec["id"],
                    pattern=_compile(spec, default_flags),
                    labels=frozenset(Label(x) for x in spec.get(labels_key, ())),
                    note=spec.get("note", ""),
                    suppresses_rules=frozenset(spec.get("suppresses_rules", ())),
                    unless=_compile_unless(spec),
                )
                for spec in doc.get(key, ())
            ]

        return cls(
            version=doc["version"],
            severity={Label(k): v for k, v in doc["severity"].items()},
            supersedes={
                Label(k): frozenset(Label(x) for x in v)
                for k, v in doc.get("supersedes", {}).items()
            },
            protect=[
                _compile(spec, "")
                for spec in doc["sentence_splitter"]["protect_suffix_patterns"]
            ],
            exclusions=signals("exclusions", "__none__"),
            negations=signals("negations", "applies_to"),
            rules=[
                Rule(
                    id=spec["id"],
                    label=Label(spec["label"]),
                    pattern=_compile(spec, default_flags),
                    note=spec.get("note", ""),
                    unless=_compile_unless(spec),
                )
                for spec in doc["rules"]
            ],
            open_signals=signals("open_signals", "conflicts_with"),
        )

    def rank(self, label: Label) -> int:
        return self.severity.get(label, 0)


def resolve_rules_path(path: str | Path | None = None) -> Path:
    """Explicit path, then $JOBFILTER_RULES, then rules.json above this module."""
    if path is not None:
        return Path(path)
    env = os.environ.get(RULES_ENV_VAR)
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / RULES_FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"could not find {RULES_FILENAME}; set ${RULES_ENV_VAR} to its path"
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    label: Label
    sentence: str
    rule_ids: tuple[str, ...]
    # How many rules for THIS label fired on the sentence. Used only to break
    # ties: a section heading ("Export Control Requirements") and the sentence
    # spelling out the categories both score RESTRICTED_US_PERSON, and the
    # second one is the quotable evidence.
    strength: int


class RuleClassifier:
    """Deterministic regex classifier. Confidence is always 1.0 -- a rule either
    matched or it did not."""

    name = CLASSIFIER_NAME

    def __init__(self, ruleset: RuleSet | None = None) -> None:
        self.ruleset = ruleset or RuleSet.load()

    def classify(self, posting: Posting) -> Decision:
        return self.classify_text(posting.description_text)

    def classify_text(self, text: str) -> Decision:
        """Same logic as `classify`, on a bare string. Keeps tests readable."""
        rs = self.ruleset

        if not text or not text.strip():
            # Never silently drop: an unreadable posting is a human's problem.
            return self._decision(Label.UNCLEAR, None, ())

        candidates: list[_Candidate] = []
        open_hits: dict[Label, str] = {}  # conflicted label -> open signal id

        for sentence in split_sentences(text, rs.protect):
            excluded = _first_match(rs.exclusions, sentence)

            if excluded is None:
                for signal in rs.open_signals:
                    if signal.matches(sentence):
                        for label in signal.labels:
                            open_hits.setdefault(label, signal.id)

            candidate = self._classify_sentence(sentence, excluded is not None)
            if candidate is not None:
                candidates.append(candidate)

        if not candidates:
            return self._decision(Label.OPEN, None, ())

        winner = max(enumerate(candidates), key=_ranked(rs))[1]

        conflict = open_hits.get(winner.label)
        if conflict is not None:
            # The posting contradicts itself. Say so rather than picking a side.
            return self._decision(
                Label.UNCLEAR, winner.sentence, winner.rule_ids + (conflict,)
            )

        return self._decision(winner.label, winner.sentence, winner.rule_ids)

    def _classify_sentence(self, sentence: str, excluded: bool) -> _Candidate | None:
        rs = self.ruleset

        negated: set[Label] = set()
        cancelled: set[str] = set()
        for signal in rs.negations:
            if signal.matches(sentence):
                negated |= signal.labels
                cancelled |= signal.suppresses_rules

        matches = [
            rule
            for rule in rs.rules
            if rule.label not in negated
            and rule.id not in cancelled
            and rule.matches(sentence)
        ]
        if not matches:
            return None

        labels = {rule.label for rule in matches}
        for label in list(labels):
            labels -= rs.supersedes.get(label, frozenset())

        if excluded:
            # EEO boilerplate that also carries a restriction: do not drop it,
            # do not trust it either.
            if any(label.is_restriction for label in labels):
                ids = tuple(r.id for r in matches if r.label in labels)
                return _Candidate(Label.UNCLEAR, sentence, ids, len(ids))
            return None

        best = max(labels, key=rs.rank)
        ids = tuple(rule.id for rule in matches if rule.label == best)
        others = tuple(rule.id for rule in matches if rule.label != best)
        return _Candidate(best, sentence, ids + others, len(ids))

    def _decision(
        self, label: Label, evidence: str | None, rule_ids: Iterable[str]
    ) -> Decision:
        ids = tuple(rule_ids)
        return Decision(
            label=label,
            confidence=1.0,
            evidence=_clean_evidence(evidence),
            matched_rule=ids[0] if ids else None,
            classifier_name=self.name,
            matched_rules=ids,
        )


def _clean_evidence(sentence: str | None) -> str | None:
    """Drop the bullet marker the HTML renderer added.

    `- ` in front of a list item is our own rendering, not the posting's words,
    so quoting it back would misreport the source.
    """
    if sentence is None:
        return None
    return sentence.removeprefix("- ").strip() or None


def _ranked(ruleset: RuleSet):
    """Order candidates: most restrictive, then best-evidenced, then earliest.

    After severity, prefer the sentence where more rules fired, then the longer
    one. Both favour a sentence that states the restriction over a heading that
    merely names it.
    """

    def key(pair: tuple[int, _Candidate]) -> tuple[int, int, int, int]:
        index, candidate = pair
        return (
            ruleset.rank(candidate.label),
            candidate.strength,
            len(candidate.sentence),
            -index,
        )

    return key


def _first_match(signals: list[Signal], sentence: str) -> Signal | None:
    return next((s for s in signals if s.matches(sentence)), None)
