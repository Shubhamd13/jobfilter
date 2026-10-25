"""How much of a posting is about the things you already do.

The verdict says whether a posting is in reach (the right kind of work, few
enough years). This says how close it is to your own experience, so the Inbox
can lead with the postings that name your tools. It is the personal half
again: the skills and their weights live in the profile, and this module only
counts them.

Each skill is a name, a pattern and a weight. A skill counts once however
often the posting mentions it, so a long posting does not win by repetition.
The score is the sum of the weights of the skills found, and the names found
are kept as the evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    pattern: re.Pattern[str]
    weight: int

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Skill:
        return cls(
            name=doc["name"],
            pattern=re.compile(doc["pattern"], re.IGNORECASE),
            weight=int(doc.get("weight", 1)),
        )


@dataclass(frozen=True, slots=True)
class SkillMatch:
    """`skills` are the names found, heaviest first: the reason for the score."""

    score: int = 0
    skills: tuple[str, ...] = ()


def skill_match(
    title: str | None, description: str | None, skills: Sequence[Skill]
) -> SkillMatch:
    """Which of `skills` the posting mentions, in its title or its text."""
    text = f"{title or ''}\n{description or ''}"
    found = [skill for skill in skills if skill.pattern.search(text)]
    found.sort(key=lambda skill: -skill.weight)  # stable: profile order within a weight
    return SkillMatch(sum(skill.weight for skill in found), tuple(skill.name for skill in found))
