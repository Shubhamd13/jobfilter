"""Run the role funnel and the model over the database, and store the results.

The funnel (funnel.py) and the verdict (reader.judge) are pure; this is the one
place they meet storage. Two operations:

  * `screen_store` places every posting and, for those a model has read, turns
    the stored facts into a verdict. It replaces the previous screening whole:
    placements depend on the rules, the profile and every company's other
    postings, so there is nothing to patch. Fast, and it calls no model.
  * `read_store` has a model read the postings that passed the rules and have
    no reading yet, storing each as it lands, then screens again.

A reading belongs to the text it was made from: when a posting's description
changes, its facts go stale and it is read again.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..storage import RoleScreen, Store
from .funnel import Candidate, Placement, Stage, screen
from .match import SkillMatch, skill_match
from .profile import Profile
from .reader import PROMPT_VERSION, Reader, RoleFacts, Verdict, judge
from .rules import RoleRules

# After this many failures in a row the model is taken to be down.
MAX_FAILURES_IN_A_ROW = 3


def screen_store(
    store: Store, rules: RoleRules | None = None, profile: Profile | None = None
) -> dict[str, int]:
    """Screen every stored posting. Returns postings per stage, every stage listed.

    Rules and profile are loaded fresh by default, so an edit to profile.json
    takes effect on the next screening without a restart.
    """
    rules = rules or RoleRules.load()
    profile = profile or Profile.load()

    inputs = store.screen_inputs()
    candidates = [_candidate(row) for row in inputs]
    placements = screen(candidates, rules, profile)
    readings = store.role_facts()

    store.replace_role_screens(
        (
            _stored(row, placement, _fresh(readings.get(row["posting_id"]), row), profile)
            for row, placement in zip(inputs, placements)
        ),
        rules_version=rules.version,
    )

    counts = Counter(str(p.stage) for p in placements)
    return {str(stage): counts[str(stage)] for stage in Stage}


@dataclass(frozen=True, slots=True)
class ReadSummary:
    """What one `read_store` run did."""

    read: int
    failed: int
    waiting: int  # passed the rules, still without a reading
    seconds: float
    stopped: str | None = None  # why it stopped early, if it did

    @property
    def per_posting(self) -> float:
        return self.seconds / self.read if self.read else 0.0


Progress = Callable[[int, int, float], None]


def read_store(
    store: Store,
    reader: Reader,
    *,
    rules: RoleRules | None = None,
    profile: Profile | None = None,
    limit: int | None = None,
    companies: Sequence[str] = (),
    redo: bool = False,
    progress: Progress | None = None,
) -> ReadSummary:
    """Have `reader` read the postings that passed the rules; then screen again.

    Only postings without a current reading are read, unless `redo`. Each
    reading is stored as it lands, so a stopped run loses nothing. A reading
    that failed is not stored: the posting stays unread and is tried next time.
    """
    rules = rules or RoleRules.load()
    profile = profile or Profile.load()

    inputs = store.screen_inputs()
    placements = screen([_candidate(row) for row in inputs], rules, profile)
    readings = {} if redo else store.role_facts()
    todo = [
        row
        for row, placement in zip(inputs, placements)
        if placement.needs_model
        and (not companies or row["company_slug"] in companies)
        and _fresh(readings.get(row["posting_id"]), row, reader.name) is None
    ]
    batch = todo[:limit] if limit is not None else todo

    read = failed = in_a_row = 0
    stopped: str | None = None
    started = time.monotonic()
    for row in batch:
        began = time.monotonic()
        facts = reader.read(row["title"] or "", row["description_text"] or "")
        if facts.error:
            failed += 1
            in_a_row += 1
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                stopped = facts.error
                break
            continue
        in_a_row = 0
        store.set_role_facts(
            row["posting_id"],
            reader=reader.name,
            prompt_version=PROMPT_VERSION,
            content_hash=row["content_hash"],
            facts=facts.to_dict(),
            seconds=round(time.monotonic() - began, 2),
        )
        read += 1
        if progress is not None:
            progress(read + failed, len(batch), time.monotonic() - started)

    seconds = time.monotonic() - started
    screen_store(store, rules, profile)
    return ReadSummary(read, failed, len(todo) - read, seconds, stopped)


def _candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(
        posting_id=row["posting_id"],
        company=row["company_slug"],
        title=row["title"],
        location=row["location"],
        label=row["label"],
        description=row["description_text"],
    )


def _fresh(
    reading: dict[str, Any] | None, row: dict[str, Any], reader: str | None = None
) -> RoleFacts | None:
    """The stored facts, if they were read from this text with this prompt.

    `reader` narrows it to one model's readings: what `read_store` uses to
    decide a posting still needs reading. A verdict accepts any model's.
    """
    if reading is None:
        return None
    if reading["content_hash"] != row["content_hash"]:
        return None
    if reading["prompt_version"] != PROMPT_VERSION:
        return None
    if reader is not None and reading["reader"] != reader:
        return None
    return RoleFacts.from_dict(reading["facts"])


def _stored(
    row: dict[str, Any], placement: Placement, facts: RoleFacts | None, profile: Profile
) -> RoleScreen:
    verdict = reason = None
    match = SkillMatch()
    if placement.needs_model:
        # Scored whether or not a model has read it: an unread posting that
        # names your tools should still sort above one that does not.
        match = skill_match(row["title"], row["description_text"], profile.skills)
    if placement.needs_model and facts is not None and profile.fit is not None:
        decided, reason = judge(facts, profile.fit)
        if (
            decided is Verdict.FIT
            and profile.best_fit_score is not None
            and match.score >= profile.best_fit_score
        ):
            decided = Verdict.BEST
        verdict = str(decided)

    focus = placement.focus
    return RoleScreen(
        posting_id=row["posting_id"],
        stage=str(placement.stage),
        reason=placement.reason,
        evidence=placement.evidence,
        category=placement.category,
        rule_id=placement.rule_id,
        focus_by=focus.by if focus else None,
        focus_evidence=focus.evidence if focus else None,
        verdict=verdict,
        verdict_reason=reason,
        match_score=match.score,
        match_skills=match.skills,
    )
