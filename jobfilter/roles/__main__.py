"""Print the screening funnel over the stored postings. Reads the database only.

    python -m jobfilter.roles                    # how many postings stop at each stage
    python -m jobfilter.roles --show mech_elec   # examples a stage removed, with the reason
    python -m jobfilter.roles --show model -n 50 # what is left for the model
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Sequence

from ..storage import default_db_path
from .funnel import Candidate, Placement, Stage, screen
from .profile import Profile, resolve_profile_path
from .rules import RoleRules

_STAGE_TEXT = {
    Stage.LOCATION: "outside your locations",
    Stage.RESTRICTED: "restricted label",
    Stage.NON_ENGINEERING: "non-engineering title",
    Stage.SENIORITY: "seniority / student title",
    Stage.OFF_FOCUS: "not about the focus area",
    Stage.MECH_ELEC: "mechanical / electrical title",
    Stage.MODEL: "left for the model",
}


def read_postings(db: Path) -> list[sqlite3.Row]:
    """Title, location, label and description of every stored posting. Read-only."""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            """
            SELECT o.posting_id, o.company_slug, o.title, o.location, o.label,
                   p.description_text
              FROM posting_overview o JOIN postings p ON p.id = o.posting_id
             WHERE o.classifier_name IS NULL OR o.classifier_name = 'rules'
             ORDER BY o.posting_id
            """
        ).fetchall()
    finally:
        connection.close()


def placements(
    rows: Sequence[sqlite3.Row], rules: RoleRules, profile: Profile
) -> list[tuple[sqlite3.Row, Placement]]:
    candidates = [
        Candidate(
            posting_id=row["posting_id"],
            company=row["company_slug"],
            title=row["title"],
            location=row["location"],
            label=row["label"],
            description=row["description_text"],
        )
        for row in rows
    ]
    return list(zip(rows, screen(candidates, rules, profile)))


def render_funnel(placed: Sequence[tuple[sqlite3.Row, Placement]]) -> str:
    counts = Counter(p.stage for _, p in placed)
    reasons: dict[Stage, Counter[str]] = {stage: Counter() for stage in Stage}
    for _, p in placed:
        reasons[p.stage][p.reason] += 1

    remaining = len(placed)
    lines = [f"{remaining:6,}  stored postings"]
    for stage in Stage:
        if stage is Stage.MODEL:
            continue
        remaining -= counts[stage]
        lines.append(f"{-counts[stage]:6,}  {_STAGE_TEXT[stage]:30} -> {remaining:,} left")
        if stage not in (Stage.LOCATION, Stage.OFF_FOCUS):
            for reason, n in reasons[stage].most_common():
                lines.append(f"            {n:5,}  {reason}")
    lines.append(f"{counts[Stage.MODEL]:6,}  {_STAGE_TEXT[Stage.MODEL]}")
    return "\n".join(lines)


def render_examples(
    placed: Sequence[tuple[sqlite3.Row, Placement]], stage: Stage, limit: int, seed: int
) -> str:
    chosen = [(row, p) for row, p in placed if p.stage is stage]
    random.Random(seed).shuffle(chosen)
    lines = [f"{len(chosen):,} postings at '{stage}' ({_STAGE_TEXT[stage]}); showing {min(limit, len(chosen))}:"]
    for row, p in chosen[:limit]:
        why = p.evidence if p.focus is None else f"{p.focus.by}: {p.focus.evidence}"
        lines.append(
            f"  {(row['company_slug'] or '')[:16]:16} {(row['title'] or '')[:58]:58} "
            f"| {p.reason}" + (f" [{why}]" if why else "")
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jobfilter.roles", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--db", type=Path, default=None, help="database (default: jobfilter.db)")
    parser.add_argument("--rules", type=Path, default=None, help="role_rules.json to use")
    parser.add_argument("--profile", type=Path, default=None, help="profile to use")
    parser.add_argument("--show", choices=[str(s) for s in Stage], help="list examples from one stage")
    parser.add_argument("-n", "--limit", type=int, default=30, help="examples to list")
    parser.add_argument("--seed", type=int, default=1, help="which examples: change for another sample")
    args = parser.parse_args(argv)

    rules = RoleRules.load(args.rules)
    profile = Profile.load(args.profile)
    placed = placements(read_postings(args.db or default_db_path()), rules, profile)

    if args.show:
        print(render_examples(placed, Stage(args.show), args.limit, args.seed))
    else:
        print(f"profile: {resolve_profile_path(args.profile)}")
        print(render_funnel(placed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
