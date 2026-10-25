"""Role screening: title rules, robotics focus, and the funnel order.

Titles are real ones from the database unless marked synthetic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jobfilter.roles import Candidate, Profile, RoleRules, Stage, location_kind, place, screen
from jobfilter.roles.profile import resolve_profile_path
from jobfilter.roles.rules import resolve_role_rules_path, title_parts


@pytest.fixture(scope="module")
def rules() -> RoleRules:
    return RoleRules.load()


# A fixed profile, so editing profile.example.json to taste breaks no test.
PROFILE = {
    "locations": ["us", "remote", "unknown"],
    "labels": ["OPEN", "UNCLEAR"],
    "skip_categories": [
        "hr_recruiting", "finance_accounting", "legal", "sales_marketing", "supply_ops",
        "trades_technician", "admin_support", "business_other", "people_management",
        "staff_plus", "internship", "phd_title", "mechanical", "electrical_hardware",
        "chip_design", "manufacturing",
    ],
    "focus": ["robotics"],
}


@pytest.fixture(scope="module")
def profile() -> Profile:
    return Profile.from_dict(PROFILE)


def categories(rules: RoleRules, title: str) -> set[str]:
    return {cut.category for cut in rules.title_cuts(title)}


# A description that is plainly about robots: five sentences, several terms.
ROBOTICS_JD = "\n".join(
    [
        "You will write motion planning software for our humanoid robot.",
        "Experience with ROS 2 is required.",
        "You will integrate perception and state estimation.",
        "Our robots operate in warehouses.",
        "Familiarity with reinforcement learning is a plus.",
    ]
)
# What a defence company's blurb looks like: three terms, one sentence.
BOILERPLATE_JD = (
    "We bring autonomy, computer vision and sensor fusion to the mission.\n"
    "You will manage purchase orders.\nYou will work with suppliers."
)


# --------------------------------------------------------------------------
# The rules file itself
# --------------------------------------------------------------------------

def test_the_rules_file_is_well_formed() -> None:
    doc = json.loads(resolve_role_rules_path().read_text())
    ids = [rule["id"] for rule in doc["title_rules"]]

    assert len(ids) == len(set(ids)), "rule ids must be unique"
    for rule in doc["title_rules"]:
        assert rule["category"] in doc["categories"], rule["id"]
        assert rule.get("unless_set", "engineering_title") in doc["unless_sets"], rule["id"]
        re.compile(rule["pattern"])
    for category in doc["categories"].values():
        assert category["group"] in doc["groups"]


def test_every_category_has_a_rule(rules: RoleRules) -> None:
    assert {rule.category for rule in rules.title_rules} == set(rules.categories)


def test_the_example_profile_names_real_categories(rules: RoleRules) -> None:
    example = Profile.load(Path("profile.example.json"))
    assert example.skip_categories <= set(rules.categories)
    assert set(example.focus) <= set(rules.focus)
    assert example.locations <= {"us", "remote", "unknown", "outside_us"}


# --------------------------------------------------------------------------
# Title parts
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,expected",
    [
        ("Senior Thermal Engineer, Expeditionary AI", ["Senior Thermal Engineer", "Expeditionary AI"]),
        ("Senior Software Engineer - AV Platform", ["Senior Software Engineer", "AV Platform"]),
        ("Sr. Test Engineer (Electrical)", ["Sr. Test Engineer", "Electrical"]),
        ("PCB Designer / Layout Engineer", ["PCB Designer", "Layout Engineer"]),
        pytest.param("Senior/Staff Full-Stack Engineer", ["Senior/Staff Full-Stack Engineer"], id="bare-slash-and-hyphen-do-not-split"),
        pytest.param("", [""], id="empty"),
    ],
)
def test_title_parts(title: str, expected: list[str]) -> None:
    assert title_parts(title) == expected


# --------------------------------------------------------------------------
# Title rules: one clear case per category
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,category",
    [
        ("Technical Recruiter, Production Engineering/Operations", "hr_recruiting"),
        ("Senior Financial Analyst", "finance_accounting"),
        ("Trade Compliance Manager (R5351)", "legal"),
        ("Global Account Manager, Developer Relations - Amazon", "sales_marketing"),
        ("Senior Product Sourcing Engineer, Counter Intrusion", "supply_ops"),
        ("Composite Technician, Second Shift", "trades_technician"),
        ("Executive Assistant", "admin_support"),
        ("Technical Writer, Ghost", "business_other"),
        ("Software Verification Manager, SDK", "people_management"),
        ("Staff Guidance & Controls Engineer (R4763)", "staff_plus"),
        ("Senior Compiler Engineer", "senior"),
        ("Mechanical Engineer Intern", "internship"),
        ("NVIDIA 2027 Summer Internships: Ph.D. Engineering", "phd_title"),
        ("Senior Mechanical Engineer, Rocket Motor Systems", "mechanical"),
        ("PCB Design Layout Engineer", "electrical_hardware"),
        ("DFT Engineer - New College Grad", "chip_design"),
        ("Supplier Quality Engineer", "manufacturing"),
    ],
)
def test_title_category(rules: RoleRules, title: str, category: str) -> None:
    assert category in categories(rules, title)


def test_a_title_can_fall_in_several_categories(rules: RoleRules) -> None:
    assert {"senior", "mechanical"} <= categories(rules, "Senior Mechanical Engineer")


def test_a_cut_quotes_the_words_that_caused_it(rules: RoleRules) -> None:
    cut = next(c for c in rules.title_cuts("Senior Thermal Engineer") if c.category == "mechanical")
    assert (cut.evidence, cut.group, cut.rule_id) == ("Thermal", "mech_elec", "mechanical")


# --------------------------------------------------------------------------
# Title rules: the gotchas
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,absent",
    [
        # An engineer on a non-engineering team is still an engineer.
        pytest.param("AI Solutions Engineer, Talent Acquisition", "hr_recruiting", id="engineer-on-the-recruiting-team"),
        pytest.param("Sales Engineer", "sales_marketing", id="sales-engineer"),
        pytest.param("Modeling Engineer, Supply Chain Optimization", "supply_ops", id="supply-chain-is-the-team"),
        pytest.param("Device Driver Engineer", "trades_technician", id="driver-is-software"),
        pytest.param("Senior Engineer, Structural Analyst (R6027)", "business_other", id="analyst-who-is-an-engineer"),
        # A software role on a hardware team is still software.
        pytest.param("Software Engineer, Manufacturing Test", "manufacturing", id="software-on-manufacturing"),
        pytest.param("Avionics Software Engineer", "electrical_hardware", id="avionics-software"),
        pytest.param("Senior Firmware Verification Engineer", "chip_design", id="firmware-verification"),
        pytest.param("CAD Tools Developer", "mechanical", id="cad-tools-developer"),
        # Word boundaries.
        pytest.param("Three Rivers Site Lead", "hr_recruiting", id="hr-inside-three"),
        pytest.param("International Programs Lead", "internship", id="intern-inside-international"),
        pytest.param("Surface Finishing Lead", "electrical_hardware", id="rf-inside-surface"),
    ],
)
def test_title_is_not_in_category(rules: RoleRules, title: str, absent: str) -> None:
    assert absent not in categories(rules, title)


@pytest.mark.parametrize(
    "title,category",
    [
        # A team name must not rescue the role.
        pytest.param("Senior Thermal Engineer, Expeditionary AI / HPC Data Center", "mechanical", id="ai-team-does-not-save-thermal"),
        pytest.param("Technician 3, Quality Systems Engineering (R4956)", "trades_technician", id="engineering-team-does-not-save-technician"),
        pytest.param("Mechanical Engineer, Robotics (CONVOY)", "mechanical", id="mechanical-on-a-robotics-team"),
        pytest.param("Sr. Test Engineer (Electrical)", "electrical_hardware", id="discipline-in-brackets"),
        pytest.param("Engineering Manager, DRIVE OS", "people_management", id="engineering-manager-is-a-manager"),
    ],
)
def test_team_names_do_not_cancel_a_cut(rules: RoleRules, title: str, category: str) -> None:
    assert category in categories(rules, title)


@pytest.mark.parametrize(
    "title",
    [
        "Robotics Software Engineer",
        "Software Engineer, Rust",
        "Helix AI Engineer, Backend",
        "Flight Software Engineer",
        "Perception Engineer, Obstacle Foundation Models",
    ],
)
def test_plain_software_titles_are_left_undecided(rules: RoleRules, title: str) -> None:
    assert rules.title_cuts(title) == []


# --------------------------------------------------------------------------
# Focus
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        "Robotics Software Engineer",
        "Autonomy Engineer- Behavior and Motion Planning (R6007)",
        "Research Scientist, VLA Models - Atlas",
        "Drone Mission Solutions Engineer",
        "Senior Perception Engineer, Obstacle Foundation Models",
        "GNC Engineer",
        "Atlas Research Engineer, SLAM & Spatial AI",
        "Mission Software Engineer",
    ],
)
def test_focus_by_title(rules: RoleRules, title: str) -> None:
    found = rules.focus_match(title, "", ("robotics",))
    assert found is not None and found.by == "title"


def test_focus_by_description(rules: RoleRules) -> None:
    found = rules.focus_match("Software Engineer", ROBOTICS_JD, ("robotics",))
    assert found is not None and found.by == "description"
    assert "5 sentences" in found.evidence


def test_company_boilerplate_is_not_focus(rules: RoleRules) -> None:
    """Three robotics terms in one sentence is a company blurb, not a robotics job."""
    assert rules.focus_match("Materials Associate", BOILERPLATE_JD, ("robotics",)) is None


def test_no_focus_without_text(rules: RoleRules) -> None:
    assert rules.focus_match("Software Engineer", None, ("robotics",)) is None
    assert rules.focus_match(None, None, ("robotics",)) is None


def focus_pairs(rules: RoleRules, postings: list[tuple[str, str, str]]) -> list:
    return [(company, rules.focus_match(title, text, ("robotics",))) for company, title, text in postings]


def test_focus_companies(rules: RoleRules) -> None:
    """Most of a company's postings in focus puts all of them in focus."""
    postings = (
        [("robotco", "Software Engineer", ROBOTICS_JD)] * 4
        + [("robotco", "Backend Engineer", "Build our cloud services.")]
        + [("chipco", "Robotics Engineer", "")]
        + [("chipco", "Compiler Engineer", "Build compilers.")] * 9
        + [("tinyco", "Robotics Engineer", "")] * 2
    )
    companies = rules.focus_companies(focus_pairs(rules, postings), ("robotics",))

    assert set(companies) == {"robotco"}  # chipco: 1 of 10; tinyco: too few postings
    assert companies["robotco"].by == "company"
    assert "4 of this company's 5" in companies["robotco"].evidence


# --------------------------------------------------------------------------
# Location kinds
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "location,kind",
    [
        ("Costa Mesa, California, United States", "us"),
        ("Remote - US", "us"),
        ("Austin, TX; London", "us"),
        ("Remote", "remote"),
        ("Remote - MEX", "outside_us"),
        ("London, England, United Kingdom", "outside_us"),
        ("Reframe Systems HQ", "unknown"),
        (None, "unknown"),
    ],
)
def test_location_kind(location: str | None, kind: str) -> None:
    assert location_kind(location) == kind


# --------------------------------------------------------------------------
# The funnel: one posting, one stage, in order
# --------------------------------------------------------------------------

def placement(rules: RoleRules, profile: Profile, **overrides: object):
    """Place one posting on its own, as `screen` would with no company to lean on."""
    posting = {
        "title": "Robotics Software Engineer",
        "location": "San Francisco, CA",
        "label": "OPEN",
        "description": ROBOTICS_JD,
    }
    posting.update(overrides)
    description = posting.pop("description")
    focus = rules.focus_match(posting["title"], description, profile.focus)
    return place(focus=focus, rules=rules, profile=profile, **posting)  # type: ignore[arg-type]


def stage(rules: RoleRules, profile: Profile, **overrides: object) -> Stage:
    return placement(rules, profile, **overrides).stage


def test_a_robotics_software_role_reaches_the_model(rules: RoleRules, profile: Profile) -> None:
    placed = placement(rules, profile, location="Remote", label="UNCLEAR")
    assert placed.stage is Stage.MODEL and placed.needs_model
    assert placed.focus is not None and placed.focus.by == "title"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        pytest.param({"location": "London, England, United Kingdom"}, Stage.LOCATION, id="outside-us"),
        pytest.param({"location": "Reframe Systems HQ"}, Stage.MODEL, id="unknown-location-is-kept"),
        pytest.param({"label": "RESTRICTED_CLEARANCE"}, Stage.RESTRICTED, id="clearance"),
        pytest.param({"label": "INFO_NO_SPONSORSHIP"}, Stage.RESTRICTED, id="no-sponsorship-not-in-profile"),
        pytest.param({"label": None}, Stage.MODEL, id="never-classified-counts-as-unclear"),
        pytest.param({"title": "Technical Recruiter, Robotics"}, Stage.NON_ENGINEERING, id="recruiter"),
        pytest.param({"title": "Staff Robotics Engineer"}, Stage.SENIORITY, id="staff"),
        pytest.param({"title": "Robotics Software Engineer Intern"}, Stage.SENIORITY, id="intern"),
        pytest.param({"title": "Senior Robotics Software Engineer"}, Stage.MODEL, id="senior-goes-to-the-model"),
        pytest.param({"title": "Compiler Engineer", "description": "Build compilers."}, Stage.OFF_FOCUS, id="not-robotics"),
        pytest.param({"title": "Mechanical Engineer, Robotics"}, Stage.MECH_ELEC, id="mechanical-at-a-robot-company"),
    ],
)
def test_stage(rules: RoleRules, profile: Profile, overrides: dict, expected: Stage) -> None:
    assert stage(rules, profile, **overrides) is expected


def test_earlier_stages_win(rules: RoleRules, profile: Profile) -> None:
    """Location, then label, then title groups in order."""
    everything_wrong = {"title": "Staff Mechanical Recruiter", "label": "RESTRICTED_CITIZEN", "location": "London"}
    assert stage(rules, profile, **everything_wrong) is Stage.LOCATION
    assert stage(rules, profile, **{**everything_wrong, "location": "Austin, TX"}) is Stage.RESTRICTED
    assert stage(rules, profile, title="Staff Mechanical Recruiter") is Stage.NON_ENGINEERING
    assert stage(rules, profile, title="Staff Mechanical Engineer") is Stage.SENIORITY


def test_a_removed_posting_says_why(rules: RoleRules, profile: Profile) -> None:
    placed = placement(rules, profile, title="Senior Thermal Engineer, Robotics", location="Austin, TX")
    assert placed.stage is Stage.MECH_ELEC
    assert (placed.reason, placed.evidence, placed.category) == ("Mechanical", "Thermal", "mechanical")


def test_screen_lets_a_company_rescue_a_thin_posting(rules: RoleRules, profile: Profile) -> None:
    def candidate(i: int, company: str, title: str, text: str) -> Candidate:
        return Candidate(i, company, title, "Austin, TX", "OPEN", text)

    thin = ("Backend Engineer", "Build our cloud services.")
    batch = (
        [candidate(i, "robotco", "Software Engineer", ROBOTICS_JD) for i in range(5)]
        + [candidate(5, "robotco", *thin), candidate(6, "chipco", *thin)]
    )
    placed = screen(batch, rules, profile)

    assert [p.stage for p in placed[:5]] == [Stage.MODEL] * 5
    assert placed[5].stage is Stage.MODEL and placed[5].focus.by == "company"
    assert placed[6].stage is Stage.OFF_FOCUS


# --------------------------------------------------------------------------
# The profile decides what is skipped
# --------------------------------------------------------------------------

def test_the_profile_controls_the_cuts(rules: RoleRules, profile: Profile) -> None:
    doc = PROFILE

    keeps_staff = Profile.from_dict(
        {**doc, "skip_categories": [c for c in doc["skip_categories"] if c != "staff_plus"]}
    )
    assert stage(rules, keeps_staff, title="Staff Robotics Engineer") is Stage.MODEL

    skips_senior = Profile.from_dict({**doc, "skip_categories": doc["skip_categories"] + ["senior"]})
    assert stage(rules, skips_senior, title="Senior Robotics Software Engineer") is Stage.SENIORITY

    no_focus = Profile.from_dict({**doc, "focus": []})
    assert stage(rules, no_focus, title="Compiler Engineer", description="Build compilers.") is Stage.MODEL

    anywhere = Profile.from_dict({**doc, "locations": ["us", "remote", "unknown", "outside_us"]})
    assert stage(rules, anywhere, location="London") is Stage.MODEL

    no_sponsorship_ok = Profile.from_dict({**doc, "labels": doc["labels"] + ["INFO_NO_SPONSORSHIP"]})
    assert stage(rules, no_sponsorship_ok, label="INFO_NO_SPONSORSHIP") is Stage.MODEL


def test_profile_json_wins_over_the_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps({"locations": ["us"], "labels": ["OPEN"], "skip_categories": [], "focus": []}))
    monkeypatch.setenv("JOBFILTER_PROFILE", str(mine))

    assert resolve_profile_path() == mine
    assert Profile.load().labels == frozenset({"OPEN"})
