"""Role screening: is this the kind of job you want? Rules first, a model after."""

from .funnel import Candidate, Placement, Stage, location_kind, place, screen
from .match import Skill, SkillMatch, skill_match
from .profile import Profile
from .reader import CORE_WORK, FitProfile, OllamaReader, Reader, RoleFacts, Verdict, judge
from .rules import FocusMatch, RoleRules, TitleCut
from .screening import ReadSummary, read_store, screen_store

__all__ = [
    "CORE_WORK",
    "Candidate",
    "FitProfile",
    "FocusMatch",
    "OllamaReader",
    "Placement",
    "Profile",
    "ReadSummary",
    "Reader",
    "RoleFacts",
    "RoleRules",
    "Skill",
    "SkillMatch",
    "Stage",
    "TitleCut",
    "Verdict",
    "judge",
    "location_kind",
    "place",
    "read_store",
    "screen",
    "screen_store",
    "skill_match",
]
