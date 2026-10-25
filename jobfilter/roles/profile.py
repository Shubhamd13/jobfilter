"""The personal half of role screening: which facts about a role you skip.

role_rules.json says what a title means ("this is a finance role"); the profile
says what to do about it ("skip finance roles"). Keeping them apart is what
stops the tool being built around one person: swap the profile, not the rules.

`profile.json` is git-ignored. Without one, `profile.example.json` is used.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .match import Skill
from .reader import FitProfile

PROFILE_FILENAME = "profile.json"
EXAMPLE_FILENAME = "profile.example.json"
PROFILE_ENV_VAR = "JOBFILTER_PROFILE"


@dataclass(frozen=True, slots=True)
class Profile:
    # Location kinds to keep: "us", "remote", "unknown", "outside_us".
    locations: frozenset[str]
    # Restriction labels to keep, e.g. OPEN and UNCLEAR.
    labels: frozenset[str]
    # role_rules.json categories whose postings are skipped.
    skip_categories: frozenset[str]
    # Focus groups a posting must be about; empty means no focus requirement.
    focus: tuple[str, ...]
    # How a model's reading becomes a verdict. None: no verdicts are given.
    fit: FitProfile | None = None
    # Your own tools and topics, weighted: what a posting is scored against.
    skills: tuple[Skill, ...] = ()
    # A fit scoring at least this is a best fit. None: no posting is.
    best_fit_score: int | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> Profile:
        return cls.from_dict(json.loads(resolve_profile_path(path).read_text()))

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> Profile:
        return cls(
            locations=frozenset(doc["locations"]),
            labels=frozenset(doc["labels"]),
            skip_categories=frozenset(doc["skip_categories"]),
            focus=tuple(doc.get("focus", ())),
            fit=FitProfile.from_dict(doc["fit"]) if "fit" in doc else None,
            skills=tuple(Skill.from_dict(skill) for skill in doc.get("skills", ())),
            best_fit_score=doc.get("best_fit_score"),
        )


def resolve_profile_path(path: str | Path | None = None) -> Path:
    """Explicit path, then $JOBFILTER_PROFILE, then profile.json, then the example."""
    if path is not None:
        return Path(path)
    env = os.environ.get(PROFILE_ENV_VAR)
    if env:
        return Path(env)
    for filename in (PROFILE_FILENAME, EXAMPLE_FILENAME):
        for parent in Path(__file__).resolve().parents:
            candidate = parent / filename
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        f"could not find {PROFILE_FILENAME} or {EXAMPLE_FILENAME}; "
        f"set ${PROFILE_ENV_VAR} to a profile's path"
    )
