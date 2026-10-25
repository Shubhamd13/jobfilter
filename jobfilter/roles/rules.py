"""Role rules: what a job TITLE makes certain. Every pattern lives in role_rules.json.

Separate from the restriction classifier on purpose. That one answers "may I
apply?" from the description; this one answers "is this the kind of role I
want?" and only where the title settles it. Anything it leaves undecided goes
on to the model.

Two questions are asked of a posting:

  * `title_cuts`  -- which categories does the title put it in
                     ("Senior Mechanical Engineer" -> senior, mechanical)?
  * `focus_match` -- is it about the focus area (robotics), by title or by
                     enough distinct terms in the description?
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROLE_RULES_FILENAME = "role_rules.json"
ROLE_RULES_ENV_VAR = "JOBFILTER_ROLE_RULES"


def _compile(pattern: str, flags: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE if "i" in flags else 0)


# "Role, Team", "Role - Team", "Role (Team)", "Role / Other role", "Role: Team".
# A bare hyphen or slash does not split: "co-op", "Senior/Staff", "UI/UX".
_TITLE_BREAK = re.compile(r"\s*[,()|:]\s*|\s+[-–—/]\s+")
_SENTENCE_BREAK = re.compile(r"\n+|(?<=[.!?])\s+")


def title_parts(title: str) -> list[str]:
    """A title's role and team parts, role first. Never empty."""
    return [part for part in _TITLE_BREAK.split(title) if part.strip()] or [""]


@dataclass(frozen=True, slots=True)
class Category:
    id: str
    group: str
    name: str


@dataclass(frozen=True, slots=True)
class TitleRule:
    id: str
    category: str
    pattern: re.Pattern[str]
    # Cancels the rule when it also matches: "Sales Engineer" is not sales.
    unless: re.Pattern[str] | None = None
    # True: the pattern must be in the role (first) part, not a team name.
    role_only: bool = False
    note: str = ""

    def match(self, title: str) -> str | None:
        """The words that matched, or None.

        The title is read in parts. `unless` cancels a match when it holds for
        the matching part or for the role (first) part -- not for the title as
        a whole, where a team name ("..., Expeditionary AI") would cancel it.
        """
        parts = title_parts(title)
        for part in parts[:1] if self.role_only else parts:
            found = self.pattern.search(part)
            if found is None:
                continue
            if self.unless is not None and (
                self.unless.search(part) or self.unless.search(parts[0])
            ):
                continue
            return found.group(0)
        return None


@dataclass(frozen=True, slots=True)
class TitleCut:
    """One category a title falls in, with the words that put it there."""

    category: str
    group: str
    rule_id: str
    evidence: str


@dataclass(frozen=True, slots=True)
class Term:
    id: str
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class FocusMatch:
    """Why a posting counts as in focus. `terms` are the ids that matched."""

    group: str
    by: str  # "title", "description" or "company"
    terms: tuple[str, ...]
    evidence: str


@dataclass(frozen=True, slots=True)
class FocusGroup:
    id: str
    name: str
    title: tuple[Term, ...]
    description: tuple[Term, ...]
    # Any description term, for counting the sentences that mention one.
    any_description: re.Pattern[str]
    min_sentences: int
    min_terms: int
    # A company is a focus company when it has at least `company_min_postings`
    # postings and this share of them are in focus on their own.
    company_min_share: float
    company_min_postings: int

    def match(self, title: str, description: str) -> FocusMatch | None:
        for term in self.title:
            found = term.pattern.search(title)
            if found:
                return FocusMatch(self.id, "title", (term.id,), found.group(0))

        # Several sentences, not several terms in one: a company blurb listing
        # "autonomy, computer vision, sensor fusion" is one sentence.
        sentences = sum(
            1 for s in _SENTENCE_BREAK.split(description) if self.any_description.search(s)
        )
        if sentences < self.min_sentences:
            return None
        hits = [
            (term.id, found.group(0))
            for term in self.description
            if (found := term.pattern.search(description))
        ]
        if len(hits) < self.min_terms:
            return None
        return FocusMatch(
            self.id,
            "description",
            tuple(term_id for term_id, _ in hits),
            f"{sentences} sentences mention: " + ", ".join(words for _, words in hits),
        )

    def company_match(self, in_focus: int, total: int) -> FocusMatch | None:
        """In focus because most of the company's postings are."""
        if total < self.company_min_postings or in_focus < self.company_min_share * total:
            return None
        return FocusMatch(
            self.id, "company", (), f"{in_focus} of this company's {total} postings are in focus"
        )


@dataclass(frozen=True, slots=True)
class RoleRules:
    version: int
    groups: tuple[str, ...]
    categories: dict[str, Category]
    title_rules: tuple[TitleRule, ...]
    focus: dict[str, FocusGroup]

    @classmethod
    def load(cls, path: str | Path | None = None) -> RoleRules:
        return cls.from_dict(json.loads(resolve_role_rules_path(path).read_text()))

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> RoleRules:
        flags = doc.get("default_flags", "i")
        unless_sets = {
            name: _compile(pattern, flags)
            for name, pattern in doc.get("unless_sets", {}).items()
        }
        categories = {
            cat_id: Category(cat_id, spec["group"], spec.get("name", cat_id))
            for cat_id, spec in doc["categories"].items()
        }

        def terms(specs: list[dict[str, Any]]) -> tuple[Term, ...]:
            return tuple(
                Term(s["id"], _compile(s["pattern"], s.get("flags", flags))) for s in specs
            )

        return cls(
            version=doc["version"],
            groups=tuple(doc["groups"]),
            categories=categories,
            title_rules=tuple(
                TitleRule(
                    id=spec["id"],
                    category=spec["category"],
                    pattern=_compile(spec["pattern"], spec.get("flags", flags)),
                    unless=unless_sets[spec["unless_set"]] if "unless_set" in spec else None,
                    role_only=spec.get("where") == "role",
                    note=spec.get("note", ""),
                )
                for spec in doc["title_rules"]
            ),
            focus={
                group_id: FocusGroup(
                    id=group_id,
                    name=spec.get("name", group_id),
                    title=terms(spec.get("title", [])),
                    description=terms(spec.get("description", [])),
                    any_description=_compile(
                        "|".join(f"(?:{s['pattern']})" for s in spec.get("description", []))
                        or r"(?!)",
                        flags,
                    ),
                    min_sentences=spec["min_sentences"],
                    min_terms=spec["min_terms"],
                    company_min_share=spec["company"]["min_share"],
                    company_min_postings=spec["company"]["min_postings"],
                )
                for group_id, spec in doc.get("focus", {}).items()
            },
        )

    def title_cuts(self, title: str | None) -> list[TitleCut]:
        """Every category the title falls in, in rule order. Empty: undecided."""
        cuts: list[TitleCut] = []
        for rule in self.title_rules:
            words = rule.match(title or "")
            if words is not None:
                group = self.categories[rule.category].group
                cuts.append(TitleCut(rule.category, group, rule.id, words))
        return cuts

    def focus_match(
        self, title: str | None, description: str | None, groups: tuple[str, ...]
    ) -> FocusMatch | None:
        """The first of `groups` the posting is about on its own, or None."""
        for group_id in groups:
            found = self.focus[group_id].match(title or "", description or "")
            if found is not None:
                return found
        return None

    def focus_companies(
        self,
        postings: Iterable[tuple[str, FocusMatch | None]],
        groups: tuple[str, ...],
    ) -> dict[str, FocusMatch]:
        """company -> why it is a focus company.

        `postings` pairs each posting's company with its own `focus_match`
        result. A short "Backend Engineer" posting at a humanoid-robot company
        says little about robots; its neighbours say it for it.
        """
        totals: dict[str, int] = {}
        in_focus: dict[tuple[str, str], int] = {}
        for company, found in postings:
            totals[company] = totals.get(company, 0) + 1
            if found is not None:
                key = (company, found.group)
                in_focus[key] = in_focus.get(key, 0) + 1

        companies: dict[str, FocusMatch] = {}
        for group_id in groups:
            for company, total in totals.items():
                if company in companies:
                    continue
                found = self.focus[group_id].company_match(
                    in_focus.get((company, group_id), 0), total
                )
                if found is not None:
                    companies[company] = found
        return companies


def resolve_role_rules_path(path: str | Path | None = None) -> Path:
    """Explicit path, then $JOBFILTER_ROLE_RULES, then role_rules.json above this module."""
    if path is not None:
        return Path(path)
    env = os.environ.get(ROLE_RULES_ENV_VAR)
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ROLE_RULES_FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"could not find {ROLE_RULES_FILENAME}; set ${ROLE_RULES_ENV_VAR} to its path"
    )
