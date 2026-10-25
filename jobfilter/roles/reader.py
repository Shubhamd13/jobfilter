"""A local model reads a posting and reports facts. Code decides what they mean.

The rules stop where a title stops being certain. What is left goes to a model
running under Ollama, which is asked only for facts it can quote: what the work
is, how many years it asks for, which degrees it accepts. It is never asked
whether to apply -- a 7B model answers that badly (it read "MS or PhD" as
PhD-required), while `judge` applies the same profile to every posting.

Nothing the model says is taken on trust:

  * a quote must appear in the posting, or the fact it supports is dropped;
  * a number of years must appear in its own quote (the model once produced
    "1 year" from a sentence with no number in it), or else in the posting;
  * a fact that fails is recorded in `flags`, and `judge` turns a failed
    years figure into UNKNOWN rather than letting the posting pass as a fit.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping, Protocol

from ..fetchers import HttpClient
from ..fetchers.base import TransportFailure

# Bump when the prompt or schema changes: stored facts from an older prompt
# are read again.
PROMPT_VERSION = 1

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_URL = "http://localhost:11434/api/chat"
MODEL_ENV_VAR = "JOBFILTER_OLLAMA_MODEL"
URL_ENV_VAR = "JOBFILTER_OLLAMA_URL"
# A 7B model takes 2-3 s per posting; the first call also loads it.
DEFAULT_TIMEOUT = 180.0
# Ollama's default context (4096) would silently cut a long posting short.
NUM_CTX = 8192
# About 6,000 tokens: the longest stored posting is 15,400 characters.
MAX_DESCRIPTION_CHARS = 24_000

# What the day-to-day work is. The profile sorts these into want / stretch.
CORE_WORK: dict[str, str] = {
    "robotics_software": "Robotics software",
    "ml_ai": "ML / AI",
    "software_general": "Software",
    "embedded_firmware": "Embedded / firmware",
    "test_quality": "Test / QA",
    "systems_integration": "Systems / integration",
    "technician_ops": "Technician / operations",
    "hardware_electrical": "Electrical / hardware",
    "chip_design": "Chip design",
    "mechanical": "Mechanical",
    "manufacturing": "Manufacturing",
    "research_science": "Research science",
    "non_engineering": "Non-engineering",
}
DEGREES = ("none", "bachelors", "masters", "phd")
_DEGREE_RANK = {name: rank for rank, name in enumerate(DEGREES)}

INSTRUCTIONS = """\
You extract facts from a job posting. Do not judge whether anyone should apply.
Copy quotes EXACTLY, word for word, from the posting. If the posting does not say, use "" or null.

Fields, in order:
- core_work: the single best category for the day-to-day work of this job.
    robotics_software   = software for robots or autonomous vehicles: motion planning, controls, perception, SLAM,
                          navigation, autonomy, manipulation, robot simulation, robot learning, flight software
    ml_ai               = training, evaluating or deploying machine-learning models (not robot specific)
    software_general    = other software engineering: backend, frontend, infrastructure, cloud, tools, security,
                          graphics, data pipelines
    embedded_firmware   = firmware, device drivers, RTOS, microcontrollers
    test_quality        = software test automation, QA, validation of software or of whole systems
    systems_integration = systems engineering, integration, field or deployment engineering, flight test
    technician_ops      = technician, operator, robot operator or teleoperator, data collection, support, maintenance
    hardware_electrical = circuits, PCB, power, RF, analog, sensor hardware, electrical design or electrical test
    chip_design         = RTL, ASIC, SoC, FPGA, VLSI, EDA or CAD tools for chips, design verification, formal
                          verification, DFT, physical design, silicon validation. Use this even when the title
                          says "verification", "validation" or "automation".
    mechanical          = mechanical design, structures, thermal, CAD ownership, materials
    manufacturing       = manufacturing, production, process or quality engineering, supply chain
    research_science    = research scientist work where publishing papers is the job
    non_engineering     = writing, business, people, legal, finance, sales, program management
- years_quote: the ONE sentence that states how many years of experience are required. It must contain
    the number. "" if the posting gives no number of years.
- years_required: the number of years in years_quote. If the sentence gives different numbers for different
    degrees ("BS and 5+ years or MS with 2+ years"), give the number for a {degree} degree. If it gives a
    range ("2-4 years"), give the lower number. null if years_quote is "".
- degree_quote: the ONE sentence that states the required degree. "" if none.
- degrees_accepted: EVERY option that sentence accepts.
    "MS or PhD" -> ["masters","phd"]. "Bachelor's or higher" -> ["bachelors","masters","phd"].
    "PhD required" -> ["phd"].
    If experience can replace the degree ("or equivalent experience", "3+ years of experience or a PhD"),
    include "none". A degree that is only preferred or a plus is not required: leave it out.
    [] if degree_quote is "".
- student_only: true only if the posting says the applicant must be currently enrolled as a student.
    Otherwise false.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "core_work": {"type": "string", "enum": list(CORE_WORK)},
        "years_quote": {"type": "string"},
        "years_required": {"type": ["integer", "null"]},
        "degree_quote": {"type": "string"},
        "degrees_accepted": {
            "type": "array",
            "items": {"type": "string", "enum": list(DEGREES)},
        },
        "student_only": {"type": "boolean"},
    },
    "required": [
        "core_work", "years_quote", "years_required",
        "degree_quote", "degrees_accepted", "student_only",
    ],
}


class Verdict(StrEnum):
    # A fit that also names enough of your own skills (roles.match). `judge`
    # never returns it: screening raises a FIT to BEST.
    BEST = "best"
    FIT = "fit"
    STRETCH = "stretch"
    SKIP = "skip"
    # The model failed, or said something that could not be checked.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RoleFacts:
    """What a model read in one posting, after checking it against the text.

    `min_years` and `degrees_accepted` hold only what survived the checks;
    `flags` names what did not. `error` is set when there was no usable reply.
    """

    core_work: str | None = None
    min_years: int | None = None
    years_quote: str | None = None
    degrees_accepted: tuple[str, ...] = ()
    degree_quote: str | None = None
    student_only: bool = False
    flags: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "core_work": self.core_work,
            "min_years": self.min_years,
            "years_quote": self.years_quote,
            "degrees_accepted": list(self.degrees_accepted),
            "degree_quote": self.degree_quote,
            "student_only": self.student_only,
            "flags": list(self.flags),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> RoleFacts:
        return cls(
            core_work=doc.get("core_work"),
            min_years=doc.get("min_years"),
            years_quote=doc.get("years_quote"),
            degrees_accepted=tuple(doc.get("degrees_accepted") or ()),
            degree_quote=doc.get("degree_quote"),
            student_only=bool(doc.get("student_only")),
            flags=tuple(doc.get("flags") or ()),
            error=doc.get("error"),
        )


@dataclass(frozen=True, slots=True)
class FitProfile:
    """The personal half of the verdict: which work you want, and how senior."""

    want: frozenset[str]
    stretch: frozenset[str]
    max_years: int
    stretch_years: int
    highest_degree: str

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> FitProfile:
        return cls(
            want=frozenset(doc["want"]),
            stretch=frozenset(doc["stretch"]),
            max_years=int(doc["max_years"]),
            stretch_years=int(doc["stretch_years"]),
            highest_degree=doc["highest_degree"],
        )


class Reader(Protocol):
    """Anything that can read a posting. `name` identifies the model behind it."""

    name: str

    def read(self, title: str, description: str) -> RoleFacts: ...


class OllamaReader:
    """Reads postings with a model served by a local Ollama."""

    def __init__(
        self,
        http: HttpClient | None = None,
        *,
        model: str | None = None,
        url: str | None = None,
        degree: str = "masters",
    ) -> None:
        self.model = model or os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL
        self.url = url or os.environ.get(URL_ENV_VAR) or DEFAULT_URL
        self.name = f"ollama:{self.model}"
        self.degree = degree
        self._http = http or HttpClient(timeout=DEFAULT_TIMEOUT)

    def read(self, title: str, description: str) -> RoleFacts:
        text = description[:MAX_DESCRIPTION_CHARS]
        body = {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "options": {"num_ctx": NUM_CTX, "temperature": 0, "seed": 1},
            "messages": [
                {"role": "system", "content": INSTRUCTIONS.format(degree=_DEGREE_WORDS[self.degree])},
                {"role": "user", "content": f"JOB TITLE: {title}\n\nJOB POSTING:\n{text}"},
            ],
        }
        try:
            response = self._http.post(self.url, body)
        except TransportFailure as exc:
            return RoleFacts(error=f"could not reach Ollama: {exc}")
        if response.status_code != 200:
            return RoleFacts(error=f"Ollama answered HTTP {response.status_code}: {response.text[:200]}")
        try:
            reply = json.loads(response.json()["message"]["content"])
        except (ValueError, KeyError, TypeError):
            return RoleFacts(error="the model's reply was not the JSON asked for")
        if not isinstance(reply, dict):
            return RoleFacts(error="the model's reply was not the JSON asked for")

        facts = check(reply, description)
        if len(description) > MAX_DESCRIPTION_CHARS:
            facts = _flagged(facts, "description_truncated")
        return facts

    def close(self) -> None:
        self._http.close()


_DEGREE_WORDS = {"none": "bachelor's", "bachelors": "bachelor's", "masters": "master's", "phd": "PhD"}


# --------------------------------------------------------------------------
# Checking the model's reply against the posting
# --------------------------------------------------------------------------

def check(reply: Mapping[str, Any], description: str) -> RoleFacts:
    """Keep what the posting backs up; flag the rest. Pure."""
    flags: list[str] = []

    core_work = reply.get("core_work")
    if core_work not in CORE_WORK:
        core_work = None
        flags.append("core_work_unknown")

    years, years_quote = _checked_years(reply, description, flags)

    degrees = tuple(d for d in reply.get("degrees_accepted") or () if d in _DEGREE_RANK)
    degree_quote = _verbatim(reply.get("degree_quote"), description)
    if degrees and degree_quote is None:
        # Which degrees are required rests entirely on the sentence quoted.
        degrees = ()
        flags.append("degree_unverified")

    return RoleFacts(
        core_work=core_work,
        min_years=years,
        years_quote=years_quote,
        degrees_accepted=degrees,
        degree_quote=degree_quote if degrees else None,
        student_only=reply.get("student_only") is True,
        flags=tuple(flags),
    )


def _checked_years(
    reply: Mapping[str, Any], description: str, flags: list[str]
) -> tuple[int | None, str | None]:
    years = reply.get("years_required")
    if not isinstance(years, int) or isinstance(years, bool) or not 0 <= years <= 40:
        return None, None

    quote = _verbatim(reply.get("years_quote"), description)
    if quote is not None and re.search(rf"\b{years}\b", quote):
        return years, quote

    # The quote is missing, garbled or does not hold the number. The number
    # may still be real: accept it if the posting itself says "<n> years".
    in_posting = re.search(
        rf"[^.\n]*\b{years}\s*\+?\s*(?:or\s+more\s+)?(?:years?|yrs?)\b[^.\n]*", description, re.IGNORECASE
    )
    if in_posting:
        flags.append("years_quote_replaced")
        return years, in_posting.group(0).strip(" -•\t")

    flags.append("years_unverified")
    return None, None


def _squash(text: str) -> str:
    """Whitespace, case and quote style are not a misquote."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def _verbatim(quote: Any, description: str) -> str | None:
    """The quote, if it (or its part after a 'Heading:') is in the posting."""
    if not isinstance(quote, str) or not quote.strip():
        return None
    body = _squash(description)
    candidates = [quote, *quote.split(":", 1)[1:]]
    for candidate in candidates:
        probe = _squash(candidate).strip(" .-•")
        if len(probe) >= 8 and probe in body:
            return candidate.strip(" -•\t")
    return None


def _flagged(facts: RoleFacts, flag: str) -> RoleFacts:
    return replace(facts, flags=(*facts.flags, flag))


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------

def judge(facts: RoleFacts, fit: FitProfile) -> tuple[Verdict, str]:
    """(verdict, reason) for one posting's facts under one profile. Pure.

    SKIP when a degree above yours is required, the work is neither wanted nor
    a stretch, or it asks for more years than `stretch_years`. STRETCH for
    stretch work or `max_years` < years <= `stretch_years`. UNKNOWN when the
    model failed or a years figure could not be checked: never FIT by default.
    """
    if facts.error:
        return Verdict.UNKNOWN, facts.error
    if facts.core_work is None:
        return Verdict.UNKNOWN, "the model could not say what the work is"

    work = CORE_WORK[facts.core_work]
    degrees = facts.degrees_accepted
    if degrees and min(_DEGREE_RANK[d] for d in degrees) > _DEGREE_RANK[fit.highest_degree]:
        lowest = min(degrees, key=_DEGREE_RANK.__getitem__)
        return Verdict.SKIP, f"{_DEGREE_NAMES[lowest]} required"

    if facts.core_work not in fit.want | fit.stretch:
        return Verdict.SKIP, f"{work} work"

    if "years_unverified" in facts.flags:
        return Verdict.UNKNOWN, f"{work}; the years it asks for could not be checked"

    years = facts.min_years
    if years is not None and years > fit.stretch_years:
        return Verdict.SKIP, f"{years}+ years required"
    if years is not None and years > fit.max_years:
        return Verdict.STRETCH, f"{work}, {years}+ years required"
    asks = f"{years} years" if years is not None else "no years stated"
    if facts.core_work in fit.stretch:
        return Verdict.STRETCH, f"{work}, {asks}"
    return Verdict.FIT, f"{work}, {asks}"


_DEGREE_NAMES = {"none": "No degree", "bachelors": "Bachelor's", "masters": "Master's", "phd": "PhD"}
