"""Skill matching and the "best fit" verdict."""

from __future__ import annotations

import dataclasses
import json

import pytest

from jobfilter.models import Label
from jobfilter.roles import Profile, RoleRules, Skill, read_store, screen_store, skill_match
from jobfilter.storage import Store
from jobfilter.web import Api

from .test_reader import PROFILE_WITH_FIT, FakeReader
from .test_storage import make_posting, make_result

SKILLS = [
    {"name": "ROS 2", "pattern": r"\bros\s?2\b", "weight": 3},
    {"name": "Nav2", "pattern": r"\bnav2\b", "weight": 3},
    {"name": "Simulation", "pattern": r"\bsimulat(?:ion|or)s?\b", "weight": 2},
    {"name": "C++", "pattern": r"\bc\+\+", "weight": 1},
    {"name": "Python", "pattern": r"\bpython\b"},
]
skills = [Skill.from_dict(s) for s in SKILLS]
PROFILE = {**PROFILE_WITH_FIT, "skills": SKILLS, "best_fit_score": 6}

# Five sentences about robots, so the posting is in focus by its description.
ROBOTS = "\n".join(f"Our robots do thing {i}." for i in range(5))
NAV_JD = ROBOTS + "\nYou will extend our Nav2 stack on ROS 2.\nC++ required."   # 3 + 3 + 1 = 7
PLAIN_JD = ROBOTS + "\nYou will write C++ and Python."                           # 1 + 1 = 2


# --------------------------------------------------------------------------
# Counting skills
# --------------------------------------------------------------------------

def test_the_score_is_the_weights_of_the_skills_found() -> None:
    found = skill_match("Navigation Engineer", "Nav2 on ROS 2, in C++.", skills)
    assert found.score == 7
    assert found.skills == ("ROS 2", "Nav2", "C++"), "heaviest first, profile order within a weight"


def test_a_skill_counts_once_however_often_it_is_named() -> None:
    assert skill_match("", "Python, python and more Python.", skills).score == 1


def test_the_title_counts_too() -> None:
    assert skill_match("Simulation Engineer", "Build things.", skills).skills == ("Simulation",)


def test_matching_ignores_case_and_respects_word_edges() -> None:
    assert skill_match("", "ros2 and NAV2", skills).score == 6
    assert skill_match("", "pythonic prose about canav2", skills).score == 0


@pytest.mark.parametrize("title,text", [(None, None), ("", ""), ("Chef", "Cook dinner.")])
def test_nothing_found_scores_zero(title: str | None, text: str | None) -> None:
    found = skill_match(title, text, skills)
    assert (found.score, found.skills) == (0, ())


def test_no_skills_in_the_profile_scores_zero() -> None:
    assert skill_match("Nav2 Engineer", "ROS 2", []).score == 0


def test_a_weight_defaults_to_one_and_the_profile_carries_the_bar() -> None:
    profile = Profile.from_dict(PROFILE)
    assert [s.weight for s in profile.skills] == [3, 3, 2, 1, 1]
    assert profile.best_fit_score == 6
    assert Profile.from_dict(PROFILE_WITH_FIT).skills == () and Profile.from_dict(PROFILE_WITH_FIT).best_fit_score is None


# --------------------------------------------------------------------------
# Best fit
# --------------------------------------------------------------------------

@pytest.fixture
def store() -> Store:
    with Store(":memory:") as opened:
        yield opened


def add(store: Store, job_id: str, title: str, text: str) -> None:
    posting = dataclasses.replace(make_posting(job_id=job_id, title=title, text=text), location="Austin, TX")
    store.record_run("t", [make_result(posting, label=Label.OPEN)])


def rows(store: Store) -> dict[str, dict]:
    return {row["job_id"]: row for row in store.postings()}


def read(store: Store, profile: dict = PROFILE) -> None:
    read_store(store, FakeReader(), rules=RoleRules.load(), profile=Profile.from_dict(profile))


def test_a_fit_that_names_your_skills_is_a_best_fit(store: Store) -> None:
    add(store, "nav", "Robotics Software Engineer", NAV_JD)
    add(store, "plain", "Robotics Software Engineer", PLAIN_JD)
    read(store)

    found = rows(store)
    assert (found["nav"]["verdict"], found["nav"]["match_score"]) == ("best", 7)
    assert json.loads(found["nav"]["match_skills"]) == ["ROS 2", "Nav2", "C++"]
    assert (found["plain"]["verdict"], found["plain"]["match_score"]) == ("fit", 2)
    assert found["nav"]["verdict_reason"] == "Robotics software, 2 years", "the reason is still the fit's"


def test_only_a_fit_can_be_a_best_fit(store: Store) -> None:
    """The fake reader gives senior titles 7 years: a skip, whatever skills the posting names."""
    add(store, "senior", "Senior Robotics Software Engineer", NAV_JD)
    read(store)
    found = rows(store)["senior"]
    assert (found["verdict"], found["match_score"]) == ("skip", 7)


def test_a_posting_is_scored_before_a_model_has_read_it(store: Store) -> None:
    add(store, "nav", "Robotics Software Engineer", NAV_JD)
    screen_store(store, RoleRules.load(), Profile.from_dict(PROFILE))
    found = rows(store)["nav"]
    assert found["verdict"] is None and found["match_score"] == 7


def test_what_the_rules_removed_is_not_scored(store: Store) -> None:
    add(store, "recruiter", "Technical Recruiter", NAV_JD)
    read(store)
    found = rows(store)["recruiter"]
    assert (found["screen_stage"], found["match_score"], found["match_skills"]) == ("non_engineering", 0, "[]")


def test_the_bar_is_the_profiles_and_moving_it_needs_no_model(store: Store) -> None:
    add(store, "nav", "Robotics Software Engineer", NAV_JD)
    read(store)
    assert rows(store)["nav"]["verdict"] == "best"

    for bar, expected in ((8, "fit"), (None, "fit"), (7, "best")):
        screen_store(store, RoleRules.load(), Profile.from_dict({**PROFILE, "best_fit_score": bar}))
        assert rows(store)["nav"]["verdict"] == expected, bar


def test_best_fits_can_be_filtered_and_counted(store: Store) -> None:
    add(store, "nav", "Robotics Software Engineer", NAV_JD)
    add(store, "plain", "Robotics Software Engineer", PLAIN_JD)
    read(store)

    assert [r["job_id"] for r in store.postings(verdict=["best"])] == ["nav"]
    assert store.filtered_verdict_counts() == {"best": 1, "fit": 1}

    api = Api(store, lambda urls, expand=False: [])
    page = api.handle("GET", "/api/postings", {"verdict": ["best"]}).payload
    assert [p["job_id"] for p in page["postings"]] == ["nav"]
    assert api.handle("GET", "/api/stats").payload["verdicts"][0] == "best"
