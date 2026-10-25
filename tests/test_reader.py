"""The model step: checking what a model read, the verdict, and the runner.

No model is called here. `OllamaReader` talks to an httpx.MockTransport and
`read_store` gets a fake reader; a test against a real Ollama is marked live.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from jobfilter.cli import build_parser, read
from jobfilter.fetchers import HttpClient
from jobfilter.models import Label
from jobfilter.roles import (
    FitProfile,
    OllamaReader,
    Profile,
    RoleFacts,
    RoleRules,
    Verdict,
    judge,
    read_store,
    screen_store,
)
from jobfilter.roles.reader import NUM_CTX, PROMPT_VERSION, check
from jobfilter.storage import UNREAD, Store
from jobfilter.web import Api

from .test_applications import JOBS, seed
from .test_roles import PROFILE, ROBOTICS_JD
from .test_storage import make_posting, make_result

FIT = {
    "want": ["robotics_software", "ml_ai", "software_general"],
    "stretch": ["embedded_firmware", "test_quality", "systems_integration", "technician_ops", "research_science"],
    "max_years": 3,
    "stretch_years": 5,
    "highest_degree": "masters",
}
fit = FitProfile.from_dict(FIT)
PROFILE_WITH_FIT = {**PROFILE, "fit": FIT}

JD = """About the role
You will write motion planning software for our humanoid robot.

Requirements
- BS in Computer Science and 5+ years of experience, or MS with 2+ years' experience.
- Strong C++ and Python.

Nice to have
- PhD in Robotics is a plus.
"""


def reply(**overrides: object) -> dict:
    """A well-formed model reply for JD."""
    base = {
        "core_work": "robotics_software",
        "years_quote": "BS in Computer Science and 5+ years of experience, or MS with 2+ years' experience.",
        "years_required": 2,
        "degree_quote": "BS in Computer Science and 5+ years of experience, or MS with 2+ years' experience.",
        "degrees_accepted": ["bachelors", "masters"],
        "student_only": False,
    }
    return {**base, **overrides}


# --------------------------------------------------------------------------
# Checking the reply against the posting
# --------------------------------------------------------------------------

def test_a_reply_the_posting_backs_up_is_kept_whole() -> None:
    facts = check(reply(), JD)
    assert (facts.core_work, facts.min_years, facts.degrees_accepted) == (
        "robotics_software", 2, ("bachelors", "masters"),
    )
    assert facts.flags == () and facts.error is None
    assert "MS with 2+ years" in facts.years_quote


def test_a_quote_may_carry_its_heading() -> None:
    """'Requirements: Strong C++ and Python.' quotes the line under a heading."""
    facts = check(reply(degree_quote="Requirements: BS in Computer Science and 5+ years of experience, or MS with 2+ years' experience."), JD)
    assert facts.degrees_accepted == ("bachelors", "masters") and facts.flags == ()


def test_whitespace_case_and_quote_style_are_not_a_misquote() -> None:
    facts = check(reply(years_quote="bs in computer science and 5+ years of experience,   or ms with 2+ years’ experience"), JD)
    assert facts.min_years == 2 and facts.flags == ()


def test_an_invented_degree_quote_drops_the_degrees() -> None:
    facts = check(reply(degree_quote="A PhD in Robotics is required.", degrees_accepted=["phd"]), JD)
    assert facts.degrees_accepted == () and facts.degree_quote is None
    assert facts.flags == ("degree_unverified",)


def test_a_number_missing_from_its_quote_is_looked_for_in_the_posting() -> None:
    """The quote is real but holds no '2'; the posting does say '2+ years'."""
    facts = check(reply(years_quote="Strong C++ and Python."), JD)
    assert facts.min_years == 2
    assert "2+ years" in facts.years_quote
    assert facts.flags == ("years_quote_replaced",)


def test_a_number_the_posting_never_says_is_dropped() -> None:
    """The model once produced '1 year' from a sentence with no number in it."""
    facts = check(reply(years_quote="Strong C++ and Python.", years_required=1), JD)
    assert facts.min_years is None and facts.years_quote is None
    assert facts.flags == ("years_unverified",)


@pytest.mark.parametrize("years", [None, True, -1, 99, "5", 2.5], ids=repr)
def test_only_a_sane_whole_number_of_years_counts(years: object) -> None:
    facts = check(reply(years_required=years), JD)
    assert facts.min_years is None and "years_unverified" not in facts.flags


def test_an_unknown_category_is_flagged() -> None:
    facts = check(reply(core_work="wizardry"), JD)
    assert facts.core_work is None and "core_work_unknown" in facts.flags


def test_facts_round_trip_through_json() -> None:
    facts = check(reply(), JD)
    assert RoleFacts.from_dict(json.loads(json.dumps(facts.to_dict()))) == facts


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "facts,verdict,reason",
    [
        (RoleFacts("robotics_software"), Verdict.FIT, "Robotics software, no years stated"),
        (RoleFacts("software_general", min_years=3), Verdict.FIT, "Software, 3 years"),
        (RoleFacts("ml_ai", min_years=4), Verdict.STRETCH, "ML / AI, 4+ years required"),
        (RoleFacts("ml_ai", min_years=5), Verdict.STRETCH, "ML / AI, 5+ years required"),
        (RoleFacts("ml_ai", min_years=6), Verdict.SKIP, "6+ years required"),
        (RoleFacts("embedded_firmware", min_years=2), Verdict.STRETCH, "Embedded / firmware, 2 years"),
        (RoleFacts("technician_ops"), Verdict.STRETCH, "Technician / operations, no years stated"),
        (RoleFacts("mechanical", min_years=1), Verdict.SKIP, "Mechanical work"),
        (RoleFacts("chip_design"), Verdict.SKIP, "Chip design work"),
        (RoleFacts("robotics_software", degrees_accepted=("phd",)), Verdict.SKIP, "PhD required"),
        (RoleFacts("robotics_software", degrees_accepted=("masters", "phd")), Verdict.FIT, "Robotics software, no years stated"),
        (RoleFacts("robotics_software", degrees_accepted=("none", "phd")), Verdict.FIT, "Robotics software, no years stated"),
        (RoleFacts("robotics_software", student_only=True), Verdict.FIT, "Robotics software, no years stated"),
    ],
    ids=[
        "wanted-work", "at-the-years-limit", "one-year-over", "at-the-stretch-limit", "past-the-stretch-limit",
        "stretch-work", "technician-is-a-stretch", "unwanted-work", "chip-design", "phd-only",
        "ms-or-phd", "experience-or-phd", "internships-are-kept",
    ],
)
def test_judge(facts: RoleFacts, verdict: Verdict, reason: str) -> None:
    assert judge(facts, fit) == (verdict, reason)


def test_a_years_figure_that_failed_its_check_is_never_a_fit() -> None:
    """Dropping an unverifiable '7 years' must not turn the posting into 'no years stated'."""
    facts = RoleFacts("software_general", flags=("years_unverified",))
    verdict, reason = judge(facts, fit)
    assert verdict is Verdict.UNKNOWN and "could not be checked" in reason


def test_unwanted_work_is_skipped_before_years_are_doubted() -> None:
    assert judge(RoleFacts("mechanical", flags=("years_unverified",)), fit)[0] is Verdict.SKIP


@pytest.mark.parametrize(
    "facts",
    [RoleFacts(error="could not reach Ollama"), RoleFacts(core_work=None)],
    ids=["model-failed", "no-category"],
)
def test_no_usable_reading_is_unknown(facts: RoleFacts) -> None:
    assert judge(facts, fit)[0] is Verdict.UNKNOWN


def test_the_profile_decides_what_is_wanted() -> None:
    hardware_person = FitProfile.from_dict({**FIT, "want": ["hardware_electrical"], "highest_degree": "phd", "max_years": 10, "stretch_years": 12})
    assert judge(RoleFacts("hardware_electrical", min_years=8, degrees_accepted=("phd",)), hardware_person)[0] is Verdict.FIT
    assert judge(RoleFacts("robotics_software"), hardware_person)[0] is Verdict.SKIP


# --------------------------------------------------------------------------
# Talking to Ollama
# --------------------------------------------------------------------------

def ollama(handler) -> OllamaReader:
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _: None)
    return OllamaReader(http, model="test-model")


def answering(content: str, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": content}})

    return handler


def test_reader_sends_the_posting_and_checks_the_reply() -> None:
    seen: list[dict] = []
    reader = ollama(answering(json.dumps(reply()), seen))

    facts = reader.read("Motion Planning Engineer", JD)

    assert reader.name == "ollama:test-model"
    assert (facts.core_work, facts.min_years) == ("robotics_software", 2)
    body = seen[0]
    assert body["model"] == "test-model" and body["stream"] is False
    assert body["options"]["num_ctx"] == NUM_CTX, "Ollama's default context would cut a long posting"
    assert body["format"]["required"][0] == "core_work"
    system, user = (m["content"] for m in body["messages"])
    assert "master's degree" in system, "the ladder is read for the profile's degree"
    assert user.startswith("JOB TITLE: Motion Planning Engineer") and JD in user


@pytest.mark.parametrize(
    "handler,expected",
    [
        (answering("Sure! Here are the facts you asked for."), "not the JSON asked for"),
        (answering('["robotics_software"]'), "not the JSON asked for"),
        (lambda request: httpx.Response(200, json={"error": "model not found"}), "not the JSON asked for"),
        (lambda request: httpx.Response(404, text="model 'test-model' not found"), "HTTP 404"),
        (lambda request: httpx.Response(500, text="boom"), "could not reach Ollama"),
    ],
    ids=["prose", "wrong-shape", "no-message", "model-missing", "server-error"],
)
def test_reader_reports_a_reply_it_cannot_use(handler, expected: str) -> None:
    facts = ollama(handler).read("Engineer", JD)
    assert facts.error is not None and expected in facts.error
    assert judge(facts, fit)[0] is Verdict.UNKNOWN


def test_reader_reports_ollama_being_down() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert "could not reach Ollama" in ollama(refused).read("Engineer", JD).error


def test_a_very_long_posting_is_cut_and_says_so() -> None:
    seen: list[dict] = []
    long_jd = JD + "x" * 30_000
    facts = ollama(answering(json.dumps(reply()), seen)).read("Engineer", long_jd)
    assert "description_truncated" in facts.flags
    assert len(seen[0]["messages"][1]["content"]) < 25_000


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------

class FakeReader:
    """Answers from the title; counts what it was asked to read."""

    name = "fake:1"

    def __init__(self, fail: bool = False) -> None:
        self.titles: list[str] = []
        self.fail = fail

    def read(self, title: str, description: str) -> RoleFacts:
        self.titles.append(title)
        if self.fail:
            return RoleFacts(error="could not reach Ollama: connection refused")
        if title.startswith("Senior"):
            return RoleFacts("robotics_software", min_years=7, years_quote="7+ years")
        return RoleFacts("robotics_software", min_years=2, years_quote="2+ years")


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as opened:
        yield opened


def run(store: Store, reader: FakeReader, **kwargs):
    return read_store(
        store, reader, rules=RoleRules.load(), profile=Profile.from_dict(PROFILE_WITH_FIT), **kwargs
    )


def verdicts(store: Store) -> dict[str, str | None]:
    return {row["job_id"]: row["verdict"] for row in store.postings()}


def test_only_what_passed_the_rules_is_read(store: Store) -> None:
    seed(store)
    reader = FakeReader()
    summary = run(store, reader)

    assert sorted(reader.titles) == ["Robotics Software Engineer", "Senior Autonomy Engineer"]
    assert (summary.read, summary.failed, summary.waiting, summary.stopped) == (2, 0, 0, None)
    found = verdicts(store)
    assert (found["swe"], found["senior"]) == ("fit", "skip")
    assert found["recruiter"] is None and found["london"] is None, "rule cuts get no verdict"


def test_a_reading_is_stored_with_its_reason(store: Store) -> None:
    ids = seed(store)
    run(store, FakeReader())
    detail = store.posting(ids["senior"])
    assert (detail["verdict"], detail["verdict_reason"]) == ("skip", "7+ years required")
    assert detail["facts_reader"] == "fake:1"
    assert json.loads(detail["facts_json"])["min_years"] == 7


def test_what_was_read_is_not_read_again(store: Store) -> None:
    seed(store)
    run(store, FakeReader())
    again = FakeReader()
    summary = run(store, again)
    assert again.titles == [] and summary.read == 0
    assert verdicts(store)["swe"] == "fit", "the verdict survives the re-screen"


def test_an_edited_posting_is_read_again(store: Store) -> None:
    seed(store)
    run(store, FakeReader())
    title, location, label = JOBS["swe"]
    edited = dataclasses.replace(
        make_posting(job_id="swe", title=title, text=ROBOTICS_JD + "\nNow with more robots."), location=location
    )
    store.record_run("t", [make_result(edited, label=label)])
    screen_store(store, RoleRules.load(), Profile.from_dict(PROFILE_WITH_FIT))
    assert verdicts(store)["swe"] is None, "a reading of the old text does not count"

    again = FakeReader()
    run(store, again)
    assert again.titles == [title]
    assert verdicts(store)["swe"] == "fit"


def test_limit_company_and_redo(store: Store) -> None:
    seed(store)
    other = dataclasses.replace(
        make_posting(slug="beta", job_id="b1", title="Robotics Software Engineer", text=ROBOTICS_JD),
        location="Austin, TX",
    )
    store.record_run("t", [make_result(other, label=Label.OPEN)])

    first = FakeReader()
    summary = run(store, first, limit=1)
    assert len(first.titles) == 1 and summary.waiting == 2

    only_beta = FakeReader()
    run(store, only_beta, companies=["beta"])
    assert len(only_beta.titles) <= 1

    everything = FakeReader()
    run(store, everything, redo=True)
    assert len(everything.titles) == 3


def test_a_model_that_is_down_stops_the_run_and_stores_nothing(store: Store) -> None:
    for i in range(6):
        posting = dataclasses.replace(
            make_posting(job_id=f"r{i}", title="Robotics Software Engineer", text=ROBOTICS_JD), location="Austin, TX"
        )
        store.record_run("t", [make_result(posting, label=Label.OPEN)])
    reader = FakeReader(fail=True)
    summary = run(store, reader)

    assert len(reader.titles) == 3, "three failures in a row and it stops asking"
    assert summary.read == 0 and summary.failed == 3 and "could not reach Ollama" in summary.stopped
    assert store.role_facts() == {}
    assert set(verdicts(store).values()) == {None}


def test_a_new_profile_rejudges_without_reading(store: Store) -> None:
    seed(store)
    run(store, FakeReader())
    patient = {**PROFILE_WITH_FIT, "fit": {**FIT, "stretch_years": 10}}
    screen_store(store, RoleRules.load(), Profile.from_dict(patient))
    assert verdicts(store)["senior"] == "stretch"


def test_no_fit_section_means_no_verdicts(store: Store) -> None:
    seed(store)
    read_store(store, FakeReader(), rules=RoleRules.load(), profile=Profile.from_dict(PROFILE))
    assert set(verdicts(store).values()) == {None}
    assert len(store.role_facts()) == 2, "the readings are kept for when a fit section is added"


# --------------------------------------------------------------------------
# Filters, API, CLI
# --------------------------------------------------------------------------

def test_filter_and_count_by_verdict(store: Store) -> None:
    seed(store)
    run(store, FakeReader())
    assert [r["job_id"] for r in store.postings(verdict=["fit"])] == ["swe"]
    assert store.count_postings(verdict=[UNREAD]) == len(JOBS) - 2
    assert store.filtered_verdict_counts(stage=["model"]) == {"fit": 1, "skip": 1}


def test_api_filters_and_counts_by_verdict(store: Store) -> None:
    seed(store)
    run(store, FakeReader())
    api = Api(store, lambda urls, expand=False: [])

    page = api.handle("GET", "/api/postings", {"stage": ["model"], "verdict": ["fit", "stretch", "unknown", "unread"]}).payload
    assert [p["job_id"] for p in page["postings"]] == ["swe"]
    assert page["verdict_counts"] == {"fit": 1, "skip": 1}
    assert api.handle("GET", "/api/stats").payload["verdicts"] == ["best", "fit", "stretch", "unknown", "unread", "skip"]
    json.dumps(page)


def test_read_command(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.db"
    with Store(db) as opened:
        seed(opened)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(PROFILE_WITH_FIT))
    args = build_parser().parse_args(["read", "--db", str(db), "--profile", str(profile)])

    assert read(args, reader=FakeReader()) == 0
    out = capsys.readouterr().out
    assert "2 postings read by fake:1" in out
    assert "1  fit" in out and "1  skip" in out

    assert read(args, reader=FakeReader(fail=True)) == 0, "nothing left to read, so nothing fails"


def test_read_command_needs_a_fit_section(tmp_path: Path, capsys) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(PROFILE))
    args = build_parser().parse_args(["read", "--db", str(tmp_path / "x.db"), "--profile", str(profile)])
    assert read(args, reader=FakeReader()) == 2
    assert '"fit" section' in capsys.readouterr().err


def test_read_command_reports_a_model_that_is_down(tmp_path: Path, capsys) -> None:
    db = tmp_path / "jobs.db"
    with Store(db) as opened:
        for i in range(4):
            posting = dataclasses.replace(
                make_posting(job_id=f"r{i}", title="Robotics Software Engineer", text=ROBOTICS_JD), location="Austin, TX"
            )
            opened.record_run("t", [make_result(posting, label=Label.OPEN)])
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(PROFILE_WITH_FIT))
    args = build_parser().parse_args(["read", "--db", str(db), "--profile", str(profile)])

    assert read(args, reader=FakeReader(fail=True)) == 1
    captured = capsys.readouterr()
    assert "Stopped early" in captured.err and "4 postings that passed the rules are still unread" in captured.out


def test_the_example_profile_has_a_usable_fit_section() -> None:
    example = Profile.load(Path("profile.example.json"))
    assert example.fit is not None
    assert example.fit.want and not (example.fit.want & example.fit.stretch)
    assert example.fit.max_years <= example.fit.stretch_years


def test_the_prompt_version_is_part_of_a_reading(store: Store) -> None:
    """Facts from an older prompt are stale: bumping PROMPT_VERSION re-reads everything."""
    ids = seed(store)
    run(store, FakeReader())
    assert store.role_facts()[ids["swe"]]["prompt_version"] == PROMPT_VERSION


@pytest.mark.live
def test_a_real_model_reads_a_posting() -> None:
    facts = OllamaReader().read("Motion Planning Engineer", JD)
    assert facts.error is None
    assert facts.core_work == "robotics_software"
    assert facts.min_years == 2
