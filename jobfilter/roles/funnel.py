"""The screening funnel: where each posting stops, and why.

Stages run in a fixed order and a posting stops at the first one that removes
it. Whatever no rule removes reaches MODEL: the title did not settle it, so a
model reads the description.

    LOCATION         outside the places the profile keeps
    RESTRICTED       a citizenship / clearance label the profile does not keep
    NON_ENGINEERING  HR, finance, sales, technician...        (title)
    SENIORITY        manager, staff, intern, PhD...           (title)
    OFF_FOCUS        not about the focus area (robotics)      (title or description)
    MECH_ELEC        mechanical, electrical, chip design...   (title)
    MODEL            undecided by rules

Nothing is dropped: every posting gets a Placement, and a removed one says
which rule and which words removed it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from ..location import Region, regions
from .profile import Profile
from .rules import FocusMatch, RoleRules, TitleCut


class Stage(StrEnum):
    LOCATION = "location"
    RESTRICTED = "restricted"
    NON_ENGINEERING = "non_engineering"
    SENIORITY = "seniority"
    OFF_FOCUS = "off_focus"
    MECH_ELEC = "mech_elec"
    MODEL = "model"


# Title groups checked before and after the focus stage, in order.
_BEFORE_FOCUS = (Stage.NON_ENGINEERING, Stage.SENIORITY)
_AFTER_FOCUS = (Stage.MECH_ELEC,)

_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
# A posting never classified is treated like one the classifier could not call.
_NO_LABEL = "UNCLEAR"


@dataclass(frozen=True, slots=True)
class Placement:
    """Where a posting stopped. `evidence` is the words that decided it."""

    stage: Stage
    reason: str
    evidence: str | None = None
    category: str | None = None
    rule_id: str | None = None
    focus: FocusMatch | None = None

    @property
    def needs_model(self) -> bool:
        return self.stage is Stage.MODEL


def location_kind(location: str | None) -> str:
    """"us", "outside_us", "remote" or "unknown".

    A US part wins ("Austin, TX; London" is a US posting). "Remote" counts only
    when no country is named: "Remote - MEX" is outside the US.
    """
    found = regions(location)
    if Region.US in found:
        return "us"
    if Region.OUTSIDE_US in found:
        return "outside_us"
    return "remote" if _REMOTE.search(location or "") else "unknown"


@dataclass(frozen=True, slots=True)
class Candidate:
    """What the funnel reads of one posting."""

    posting_id: int
    company: str
    title: str | None
    location: str | None
    label: str | None
    description: str | None


def place(
    *,
    title: str | None,
    location: str | None,
    label: str | None,
    focus: FocusMatch | None,
    rules: RoleRules,
    profile: Profile,
) -> Placement:
    """Where one posting stops.

    `focus` is why the posting counts as in focus, already worked out: its own
    `RoleRules.focus_match`, or its company's entry in `focus_companies`.
    `screen` does that for a batch.
    """
    kind = location_kind(location)
    if kind not in profile.locations:
        return Placement(Stage.LOCATION, f"location is {kind}", location or None)

    label = label or _NO_LABEL
    if label not in profile.labels:
        return Placement(Stage.RESTRICTED, label)

    cuts = [c for c in rules.title_cuts(title) if c.category in profile.skip_categories]

    removed = _first_cut(cuts, _BEFORE_FOCUS, rules)
    if removed is not None:
        return removed

    if profile.focus and focus is None:
        return Placement(Stage.OFF_FOCUS, "not about " + " / ".join(profile.focus))

    removed = _first_cut(cuts, _AFTER_FOCUS, rules)
    if removed is not None:
        return removed

    return Placement(Stage.MODEL, "undecided by rules", focus=focus)


def screen(
    candidates: Sequence[Candidate], rules: RoleRules, profile: Profile
) -> list[Placement]:
    """Place every candidate, in order. Company focus is judged over the whole batch."""
    own = [rules.focus_match(c.title, c.description, profile.focus) for c in candidates]
    companies = rules.focus_companies(
        zip((c.company for c in candidates), own), profile.focus
    )
    return [
        place(
            title=c.title,
            location=c.location,
            label=c.label,
            focus=found or companies.get(c.company),
            rules=rules,
            profile=profile,
        )
        for c, found in zip(candidates, own)
    ]


def _first_cut(
    cuts: list[TitleCut], stages: tuple[Stage, ...], rules: RoleRules
) -> Placement | None:
    for stage in stages:
        for cut in cuts:
            if cut.group == stage:
                return Placement(
                    stage,
                    rules.categories[cut.category].name,
                    cut.evidence,
                    cut.category,
                    cut.rule_id,
                )
    return None
