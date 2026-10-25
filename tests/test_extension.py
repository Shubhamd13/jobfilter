"""The browser extension, tested in the engine it runs in: headless Chrome.

Four layers:
  * extraction -- saved LinkedIn / Indeed / generic pages -> posting fields,
    including which selector produced each one, so a site redesign fails a
    test that names the dead selector;
  * text parity -- the JS HTML-to-text must split lines like the Python one,
    or the two classifiers see different sentences;
  * engine -- local verdict, server verdict, server down, recording, all
    against a fake fetch;
  * service worker, badge and content script -- against a fake `chrome`.

The manifest and the files it names are checked in plain Python.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest

from jobfilter.classifiers import resolve_rules_path
from jobfilter.fetchers.textutil import html_to_text

from .conftest import FIXTURES, load_fixture
from .jsrunner import find_chrome, run_js

EXT = Path("extension")
SRC = EXT / "src"
PAGES = FIXTURES / "extension"

needs_chrome = pytest.mark.skipif(
    find_chrome() is None, reason="needs Chrome/Chromium to run the extension's JS"
)


def rules_doc() -> dict:
    return json.loads(resolve_rules_path().read_text())


# --------------------------------------------------------------------------
# Extraction from saved pages
# --------------------------------------------------------------------------

# Every fixture, what it must yield, and the selector expected to produce the
# description. When LinkedIn or Indeed redesign, the `description_from`
# assertion is the one that fails, and its message lists what was tried.
PAGE_CASES = [
    dict(
        fixture="linkedin_job_view.html",
        url="https://www.linkedin.com/jobs/view/4012345678/?refId=abc",
        site="linkedin",
        job_id="4012345678",
        canonical="https://www.linkedin.com/jobs/view/4012345678/",
        title="Robotics Software Engineer",
        company="Acme Robotics",
        location="Los Angeles, CA",
        posted_ago="2 days ago",
        description_from="#job-details",
        label="RESTRICTED_US_PERSON",
        evidence="Due to export control requirements, applicants must be U.S. persons.",
    ),
    dict(
        fixture="linkedin_search_pane.html",
        url="https://www.linkedin.com/jobs/search/?currentJobId=3998877665&keywords=robotics",
        site="linkedin",
        job_id="3998877665",
        canonical="https://www.linkedin.com/jobs/view/3998877665/",
        title="Flight Software Engineer",
        company="Orbital Dynamics",
        location="Colorado Springs, CO",
        posted_ago="Reposted 1 week ago",
        description_from="#job-details",
        label="RESTRICTED_CLEARANCE",
        evidence="An active TS/SCI clearance is required.",
    ),
    dict(
        fixture="linkedin_guest.html",
        url="https://www.linkedin.com/jobs/view/perception-engineer-at-acme-vision-3871234567?trk=public",
        site="linkedin",
        job_id="3871234567",
        canonical="https://www.linkedin.com/jobs/view/3871234567/",
        title="Perception Engineer",
        company="Acme Vision",
        location="Boston, MA",
        description_from=".show-more-less-html__markup",
        label="INFO_NO_SPONSORSHIP",
        evidence="We are unable to sponsor visas for this role.",
    ),
    dict(
        fixture="linkedin_jsonld_only.html",
        url="https://www.linkedin.com/jobs/view/4000000001/",
        site="linkedin",
        job_id="4000000001",
        canonical="https://www.linkedin.com/jobs/view/4000000001/",
        title="Controls Engineer",
        company="Ironclad Motion",
        location="Huntsville, AL",
        description_from="json-ld",
        label="RESTRICTED_US_PERSON",
        evidence="This position is subject to ITAR.",
    ),
    dict(
        fixture="linkedin_heading_only.html",
        url="https://www.linkedin.com/jobs/view/4000000002/",
        site="linkedin",
        job_id="4000000002",
        canonical="https://www.linkedin.com/jobs/view/4000000002/",
        title="Test Engineer",
        company=None,
        location=None,
        description_from="heading",
        label="RESTRICTED_CLEARANCE",
        evidence="This position requires a security clearance.",
    ),
    dict(
        fixture="linkedin_sdui_job_view.html",
        url="https://www.linkedin.com/jobs/view/4471000001/?trk=eml-email_job_alert_digest_01",
        site="linkedin",
        job_id="4471000001",
        canonical="https://www.linkedin.com/jobs/view/4471000001/",
        title="Motion Planning Engineer - Autonomy",
        company="Acme Robotics",
        location="Pittsburgh, PA",
        posted_ago="2 days ago",
        applicants="40 people clicked apply",
        description_from="#JobDetails_AboutTheJob_{jobId}",
        label="RESTRICTED_US_PERSON",
        evidence="Applicants must be a U.S. person as defined by ITAR.",
    ),
    dict(
        fixture="linkedin_sdui_search_results.html",
        url="https://www.linkedin.com/jobs/search-results/?currentJobId=4448000001&keywords=Robotics%20Engineer",
        site="linkedin",
        job_id="4448000001",
        canonical="https://www.linkedin.com/jobs/view/4448000001/",
        title="Field Robotics Engineer",
        company="Lastmile Bots",
        location="Palo Alto, CA",
        posted_ago="2 months ago",
        description_from="#JobDetails_AboutTheJob_{jobId}",
        label="INFO_NO_SPONSORSHIP",
        evidence="We are unable to sponsor visas for this role.",
    ),
    dict(
        fixture="indeed_viewjob.html",
        url="https://www.indeed.com/viewjob?jk=a1b2c3d4e5f60718&from=serp",
        site="indeed",
        job_id="a1b2c3d4e5f60718",
        canonical="https://www.indeed.com/viewjob?jk=a1b2c3d4e5f60718",
        title="Embedded Engineer",
        company="Saguaro Systems",
        location="Tucson, AZ 85701",
        description_from="#jobDescriptionText",
        label="RESTRICTED_CITIZEN",
        evidence="This position is open to U.S. citizens only.",
    ),
    dict(
        fixture="indeed_search_pane.html",
        url="https://www.indeed.com/jobs?q=robotics&l=AZ&vjk=0f1e2d3c4b5a6978",
        site="indeed",
        job_id="0f1e2d3c4b5a6978",
        canonical="https://www.indeed.com/viewjob?jk=0f1e2d3c4b5a6978",
        title="Robot Technician",
        company="Mesa Automation",
        location="Phoenix, AZ",
        description_from='[data-testid="jobsearch-JobComponent-description"]',
        label="OPEN",
        evidence=None,
    ),
    dict(
        fixture="indeed_rnw_search_pane.html",
        url="https://www.indeed.com/jobs?q=robotics+simulation&l=United+States&vjk=9a8b7c6d5e4f3021",
        site="indeed",
        job_id="9a8b7c6d5e4f3021",
        canonical="https://www.indeed.com/viewjob?jk=9a8b7c6d5e4f3021",
        title="Simulation Engineer",
        company="Keystone Robotics",
        location="Pittsburgh, PA",
        description_from='[data-testid="vj-job-description-heading"] + div',
        label="RESTRICTED_CLEARANCE",
        evidence="This position requires a security clearance.",
    ),
    # Real pages (2026-10), scripts and styles stripped. The full job page,
    # and the pane that opens beside Workday's search results.
    dict(
        fixture="workday_job_view.html",
        url="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Robotics-Research-Engineer---Robot-Simulation-and-Evaluation_JR2023397",
        site="workday",
        job_id="JR2023397",
        canonical="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Robotics-Research-Engineer---Robot-Simulation-and-Evaluation_JR2023397",
        title="Robotics Research Engineer - Robot Simulation and Evaluation",
        company="nvidia",
        location="US, CA, Santa Clara",
        posted_ago="Posted 30+ Days Ago",
        description_from='[data-automation-id="jobPostingPage"] [data-automation-id="jobPostingDescription"]',
        label="OPEN",
        evidence=None,
    ),
    dict(
        fixture="workday_search_pane.html",
        url="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/details/Senior-Systems-Software-Engineer--CUDA-Driver---Multi-Node-and-Memory-Model_JR2014447?locationHierarchy1=2fcb99c455831013ea52fb338f2932d8",
        site="workday",
        job_id="JR2014447",
        canonical="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Senior-Systems-Software-Engineer--CUDA-Driver---Multi-Node-and-Memory-Model_JR2014447",
        title="Senior Systems Software Engineer, CUDA Driver - Multi-Node and Memory Model",
        company="nvidia",
        location="US, CA, Santa Clara; US, TX, Austin; US, Remote",
        posted_ago="Posted Today",
        description_from='[data-automation-id="jobDetails"] [data-automation-id="jobPostingDescription"]',
        label="OPEN",
        evidence=None,
    ),
    dict(
        fixture="generic_jsonld.html",
        url="https://careers.heliosdefense.example/jobs/R-10442#apply",
        site="web",
        job_id="R-10442",
        canonical="https://careers.heliosdefense.example/jobs/R-10442",
        title="Systems Engineer II",
        company="Helios Defense",
        location="Arlington, VA, US",
        description_from="json-ld",
        label="RESTRICTED_CITIZEN",
        evidence="U.S. citizenship is required for this role.",
    ),
    dict(
        fixture="generic_plain.html",
        url="https://beaconlabs.example/careers/firmware-engineer",
        site="web",
        job_id=None,
        canonical="https://beaconlabs.example/careers/firmware-engineer",
        title="Firmware Engineer",
        company="Beacon Labs",
        location=None,
        description_from='[class*="job-description" i]',
        label="OPEN",
        evidence=None,
    ),
]

# Text that sits on the page but is NOT the posting: the job list beside it,
# "People also viewed", buttons, inline scripts. None may reach the classifier.
MUST_NOT_LEAK = {
    "linkedin_job_view.html": [
        "TS/SCI", "Easy Apply", "See less", "see more", "window.__li", "Messaging",
    ],
    "linkedin_search_pane.html": ["Applicants must be a US citizen", "Perception Engineer"],
    "linkedin_guest.html": ["Show more", "JSON-LD copy"],
    "indeed_viewjob.html": ["Apply now", "Company reviews", "$120,000"],
    "indeed_search_pane.html": ["lawful permanent resident"],
    "generic_plain.html": ["Share this job", "equal opportunity employer"],
    "linkedin_sdui_job_view.html": [
        "Decoy", "TS/SCI", "Easy Apply", "more", "Messaging", "window.__como",
    ],
    "linkedin_sdui_search_results.html": [
        "Decoy", "lawful permanent resident", "Meet the hiring team", "Stale Corp",
    ],
    "workday_job_view.html": [
        "Senior Robotics Research Engineer, Robotics and AI for Drug Discovery",
        "CAREERS AT NVIDIA", "Similar Jobs", "Posted 30+ Days Ago", "job requisition id",
    ],
    "workday_search_pane.html": [
        "Senior Manager, Global Payment Operations and Compliance", "1 - 20 of 1390 jobs",
        "Return to selected search result", "Posted Today", "US, TX, Austin",
    ],
    "indeed_rnw_search_pane.html": [
        "lawful permanent resident", "Apply on company site", "Prefetched", "@layer",
    ],
}

EXTRACT_SCRIPTS = [SRC / "classifier.js", SRC / "extract.js"]


@pytest.fixture(scope="module")
def extracted() -> dict[str, dict]:
    """Extract and classify every fixture page in one Chrome run."""
    pages = {c["fixture"]: [(PAGES / c["fixture"]).read_text(), c["url"]] for c in PAGE_CASES}
    pages["linkedin_feed.html"] = [
        (PAGES / "linkedin_feed.html").read_text(),
        "https://www.linkedin.com/feed/",
    ]
    return run_js(
        EXTRACT_SCRIPTS,
        {"RULES": rules_doc(), "PAGES": pages},
        """
        const clf = JobFilterRules.RuleClassifier(RULES);
        const out = {};
        for (const [name, [markup, url]] of Object.entries(PAGES)) {
          const doc = new DOMParser().parseFromString(markup, "text/html");
          // Known sites ignore `generic`; the two generic fixtures need it.
          const result = JobFilterExtract.extractPosting(doc, url, { generic: true });
          if (result.ok) result.decision = clf.classify(result.text);
          out[name] = result;
        }
        return out;
        """,
    )


def selectors_tried(site: str) -> str:
    """The candidate list from extract.js, quoted into failure messages."""
    source = (SRC / "extract.js").read_text()
    block = source.split(f'id: "{site}"', 1)[-1].split("description: [", 1)[-1]
    return block.split("]", 1)[0].strip()


@needs_chrome
@pytest.mark.parametrize("case", PAGE_CASES, ids=lambda c: c["fixture"])
def test_page_yields_the_posting(case: dict, extracted: dict) -> None:
    got = extracted[case["fixture"]]
    assert got["ok"], f"extraction failed: {got.get('reason')} {got.get('extraction')}"
    assert got["site"] == case["site"]
    assert got["jobId"] == case["job_id"]
    assert got["url"] == case["canonical"]
    assert got["title"] == case["title"]
    assert got["company"] == case["company"]
    assert got["location"] == case["location"]
    assert got.get("postedAgo") == case.get("posted_ago")
    assert got.get("applicants") == case.get("applicants")


@needs_chrome
@pytest.mark.parametrize("case", PAGE_CASES, ids=lambda c: c["fixture"])
def test_description_comes_from_the_expected_selector(case: dict, extracted: dict) -> None:
    got = extracted[case["fixture"]]["extraction"]["description"]
    assert got == case["description_from"], (
        f"{case['fixture']}: description came from {got!r}, expected "
        f"{case['description_from']!r}. If the site changed its markup, the "
        f"selectors tried were:\n{selectors_tried(case['site'])}"
    )


@needs_chrome
@pytest.mark.parametrize("case", PAGE_CASES, ids=lambda c: c["fixture"])
def test_page_is_classified(case: dict, extracted: dict) -> None:
    decision = extracted[case["fixture"]]["decision"]
    assert decision["label"] == case["label"]
    assert decision["evidence"] == case["evidence"]


@needs_chrome
@pytest.mark.parametrize("fixture", sorted(MUST_NOT_LEAK))
def test_page_chrome_does_not_leak_into_the_text(fixture: str, extracted: dict) -> None:
    text = extracted[fixture]["text"]
    for phrase in MUST_NOT_LEAK[fixture]:
        assert phrase not in text, f"{phrase!r} leaked into the description"


@needs_chrome
def test_a_non_job_page_is_ignored(extracted: dict) -> None:
    """The feed says "Must be a U.S. citizen" -- and is still not a posting."""
    got = extracted["linkedin_feed.html"]
    assert got == {"ok": False, "site": "linkedin", "reason": "not-a-job-page"}


@needs_chrome
def test_extraction_edge_cases() -> None:
    view = (PAGES / "linkedin_job_view.html").read_text()
    out = run_js(
        EXTRACT_SCRIPTS,
        {"VIEW": view},
        """
        const X = JobFilterExtract;
        const parse = (m) => new DOMParser().parseFromString(m, "text/html");

        // The newest selector dies: the next candidate takes over.
        const renamed = parse(VIEW);
        renamed.getElementById("job-details").removeAttribute("id");
        const fallback = X.extractPosting(renamed, "https://www.linkedin.com/jobs/view/4012345678/");

        // A job page with no readable description is reported, not dropped.
        const empty = parse(VIEW);
        empty.querySelector(".jobs-description__container").remove();
        const unreadable = X.extractPosting(empty, "https://www.linkedin.com/jobs/view/4012345678/");

        return {
          fallback: [fallback.extraction.description, fallback.text.includes("U.S. persons")],
          unreadable: [unreadable.ok, unreadable.reason, unreadable.title, unreadable.jobId],
          unsupported: X.extractPosting(parse(VIEW), "https://example.com/jobs/1"),
          generic: X.extractPosting(parse(VIEW), "https://example.com/jobs/1", { generic: true }).site,
          badUrl: X.extractPosting(parse(VIEW), "not a url").reason,
          // A lookalike host is not LinkedIn.
          lookalike: X.extractPosting(parse(VIEW), "https://linkedin.com.evil.example/jobs/view/4012345678/").reason,
          indeedRegional: X.extractPosting(parse(VIEW), "https://uk.indeed.com/viewjob?jk=a1b2c3d4e5f60718").site,
          indeedBadJk: X.extractPosting(parse(VIEW), "https://www.indeed.com/viewjob?jk=<script>").reason,
        };
        """,
    )
    assert out["fallback"] == [".jobs-description__content .jobs-box__html-content", True]
    assert out["unreadable"] == [
        False,
        "description-not-found",
        "Robotics Software Engineer",
        "4012345678",
    ]
    assert out["unsupported"] == {"ok": False, "site": None, "reason": "unsupported-site"}
    assert out["generic"] == "web"
    assert out["badUrl"] == "bad-url"
    assert out["lookalike"] == "unsupported-site"
    assert out["indeedRegional"] == "indeed"
    assert out["indeedBadJk"] == "not-a-job-page"



@needs_chrome
def test_workday_urls_and_a_pane_mid_swap() -> None:
    pane = (PAGES / "workday_search_pane.html").read_text()
    out = run_js(
        EXTRACT_SCRIPTS,
        {"PANE": pane},
        """
        const X = JobFilterExtract;
        const read = (url) => X.extractPosting(new DOMParser().parseFromString(PANE, "text/html"), url);
        const base = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite";
        const slug = "Senior-Systems-Software-Engineer--CUDA-Driver---Multi-Node-and-Memory-Model";
        const pick = (r) => [r.ok, r.reason || null, r.jobId || null, r.url || null];
        return {
          // Opened from the results list: a location segment, then /apply.
          withLocation: pick(read(`${base}/job/US-CA-Santa-Clara/${slug}_JR2014447/apply?q=1`)),
          noLocale: pick(read(`https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/details/${slug}_JR2014447`)),
          repost: pick(read(`${base}/details/${slug}_JR2014447-1`)),
          // The URL moved on to the next job; the pane has not re-rendered yet.
          midSwap: pick(read(`${base}/details/Senior-Manager--Global-Payment-Operations-and-Compliance_JR2026676`)),
          searchOnly: pick(read(`${base}?locationHierarchy1=2fcb99c455831013ea52fb338f2932d8`)),
          lookalike: pick(read(`https://myworkdayjobs.com.evil.example/x/job/${slug}_JR2014447`)),
        };
        """,
    )
    canonical = (
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/"
        "Senior-Systems-Software-Engineer--CUDA-Driver---Multi-Node-and-Memory-Model_JR2014447"
    )
    assert out["withLocation"] == [True, None, "JR2014447", canonical]
    assert out["noLocale"][:3] == [True, None, "JR2014447"]
    assert out["noLocale"][3].endswith("/NVIDIAExternalCareerSite/job/" + canonical.rsplit("/", 1)[1])
    assert out["repost"][:3] == [True, None, "JR2014447-1"]
    assert out["midSwap"] == [False, "page-shows-another-job", "JR2026676", None]
    assert out["searchOnly"] == [False, "not-a-job-page", None, None]
    assert out["lookalike"] == [False, "unsupported-site", None, None]


# --------------------------------------------------------------------------
# HTML -> text parity with the Python fetchers
# --------------------------------------------------------------------------

def parity_samples() -> dict[str, str]:
    """Real description markup from every saved API response."""
    samples: dict[str, str] = {}
    greenhouse = load_fixture("greenhouse_undercontrolroboticsinc_4396196009.json")
    # Greenhouse double-escapes; Python's fetcher unescapes once first.
    samples["greenhouse"] = html.unescape(greenhouse["content"])
    lever = load_fixture("lever_rai_dff73976-3e91-4897-af10-8d2433c63204.json")
    samples["lever-description"] = lever["description"]
    for i, section in enumerate(lever["lists"]):
        samples[f"lever-list-{i}"] = section["content"]
    board = load_fixture("ashby_board_graymatter-robotics.json")
    for job in board["jobs"]:
        if job.get("descriptionHtml"):
            samples[f"ashby-{job['id'][:8]}"] = job["descriptionHtml"]
    return samples


def lines(text: str) -> list[str]:
    """What the sentence splitter sees: the non-blank lines.

    Blank-line runs can differ by one (`<br/>` is two events to Python's
    parser, one DOM node to Chrome); the classifier skips blank lines, so
    that difference cannot change a verdict.
    """
    return [line for line in text.split("\n") if line.strip()]


LISTING_LINES = {
    "San Francisco, CA · Reposted 2 weeks ago · Over 100 applicants":
        ("Reposted 2 weeks ago", "Over 100 applicants"),
    "Santa Clara, CA · 1 day ago · 71 people clicked apply":
        ("1 day ago", "71 people clicked apply"),
    "Palo Alto, CA · 2 months ago": ("2 months ago", None),
    "Remote · Be an early applicant": (None, "Be an early applicant"),
    "Boston, MA": (None, None),
    "": (None, None),
}


@needs_chrome
def test_listing_meta_line_is_split_into_its_signals() -> None:
    got = run_js(
        EXTRACT_SCRIPTS,
        {"LINES": list(LISTING_LINES)},
        """
        return LINES.map((line) => {
          const meta = JobFilterExtract.parseListingMeta(line);
          return [meta.postedAgo, meta.applicants];
        });
        """,
    )
    assert dict(zip(LISTING_LINES, map(tuple, got))) == LISTING_LINES


@needs_chrome
def test_html_to_text_matches_python_on_real_markup() -> None:
    samples = parity_samples()
    javascript = run_js(
        [SRC / "extract.js"],
        {"SAMPLES": samples},
        """
        const out = {};
        for (const [name, markup] of Object.entries(SAMPLES)) {
          const doc = new DOMParser().parseFromString(markup, "text/html");
          out[name] = JobFilterExtract.htmlToText(doc.body);
        }
        return out;
        """,
    )
    assert len(samples) >= 5
    for name, markup in samples.items():
        assert lines(javascript[name]) == lines(html_to_text(markup)), name


# --------------------------------------------------------------------------
# Engine: local, server, server down, recording
# --------------------------------------------------------------------------

ENGINE_SCRIPTS = [SRC / "classifier.js", SRC / "transport.js"]

POSTING = {
    "site": "linkedin",
    "jobId": "4012345678",
    "url": "https://www.linkedin.com/jobs/view/4012345678/",
    "title": "Robotics Software Engineer",
    "company": "Acme Robotics",
    "location": "Los Angeles, CA",
    "postedAgo": "2 days ago",
    "applicants": "Over 100 applicants",
    "text": "We build robots.\nApplicants must be a US citizen.",
    "extraction": {"description": "#job-details"},
}

SERVER_DECISION = {
    "label": "RESTRICTED_US_PERSON",
    "confidence": 0.9,
    "evidence": "Applicants must be a US citizen.",
    "matched_rule": "llm",
    "matched_rules": ["llm"],
    "classifier_name": "ollama",
}


@pytest.fixture(scope="module")
def engine_runs() -> dict:
    """Every engine scenario in one Chrome run, each with its own fake fetch."""
    return run_js(
        ENGINE_SCRIPTS,
        {"RULES": rules_doc(), "POSTING": POSTING, "SERVER_DECISION": SERVER_DECISION},
        """
        const E = JobFilterEngine;

        // A fake fetch that records requests and answers from a script.
        function fakeFetch(answer) {
          const calls = [];
          const fetch = async (url, init) => {
            calls.push({ url, method: init && init.method, body: init && JSON.parse(init.body) });
            return answer(url, init);
          };
          return { fetch, calls };
        }
        const json = (status, payload) => ({
          ok: status >= 200 && status < 300,
          status,
          json: async () => payload,
        });

        async function scenario(settings, answer) {
          const fake = fakeFetch(answer || (() => { throw new Error("no network expected"); }));
          const engine = E.create({ rules: RULES, fetch: fake.fetch });
          const early = [];
          const final = await engine.run(POSTING, settings, (s) => early.push(s));
          return { early, final, calls: fake.calls, disagree: E.disagreement(final) };
        }

        return (async () => ({
          local: await scenario({ mode: "local" }),
          server: await scenario({ mode: "server" }, () => json(200, { decision: SERVER_DECISION })),
          serverDown: await scenario({ mode: "server" }, () => { throw new TypeError("Failed to fetch"); }),
          server500: await scenario({ mode: "server" }, () => json(500, { error: "boom" })),
          serverJunk: await scenario({ mode: "server" }, () => json(200, { nope: 1 })),
          recordLocal: await scenario(
            { mode: "local", record: true, serverUrl: "http://127.0.0.1:9999/" },
            () => json(200, { posting_id: 7, decision: SERVER_DECISION })
          ),
          remoteHost: await scenario({ mode: "server", serverUrl: "https://evil.example" }),
          localServer: [
            E.isLocalServer("http://127.0.0.1:8000"),
            E.isLocalServer("http://localhost:1234/"),
            E.isLocalServer("https://127.0.0.1:8000"),
            E.isLocalServer("http://127.0.0.1.evil.example"),
            E.isLocalServer("garbage"),
          ],
        }))();
        """,
    )


@needs_chrome
def test_local_mode_never_touches_the_network(engine_runs: dict) -> None:
    run = engine_runs["local"]
    assert run["calls"] == []
    assert run["final"]["decision"]["label"] == "RESTRICTED_CITIZEN"
    assert run["final"]["decision"]["source"] == "local"
    assert run["final"]["pending"] is False
    assert run["early"][0]["decision"] == run["final"]["decision"]


@needs_chrome
def test_server_mode_shows_local_first_then_trusts_the_server(engine_runs: dict) -> None:
    run = engine_runs["server"]
    early, final = run["early"][0], run["final"]

    assert early["pending"] is True
    assert early["decision"]["source"] == "local"

    assert final["decision"]["label"] == "RESTRICTED_US_PERSON"
    assert final["decision"]["classifierName"] == "ollama"
    assert final["decision"]["matchedRule"] == "llm"
    assert final["decision"]["source"] == "server"
    assert final["local"]["label"] == "RESTRICTED_CITIZEN"
    assert run["disagree"] is True  # worth surfacing: it is Phase 2b's data

    [call] = run["calls"]
    assert call["url"] == "http://127.0.0.1:8000/api/classify"
    assert call["method"] == "POST"
    assert call["body"] == {"text": POSTING["text"], "title": POSTING["title"]}


@needs_chrome
@pytest.mark.parametrize(
    "scenario, error",
    [
        ("serverDown", "Failed to fetch"),
        ("server500", "boom"),
        ("serverJunk", "server sent no decision"),
        ("remoteHost", "server URL must be http://127.0.0.1 or http://localhost"),
    ],
)
def test_server_failure_falls_back_to_the_local_verdict(
    engine_runs: dict, scenario: str, error: str
) -> None:
    final = engine_runs[scenario]["final"]
    assert final["decision"]["source"] == "local"
    assert final["decision"]["label"] == "RESTRICTED_CITIZEN"
    assert final["serverError"] == error
    assert final["pending"] is False
    assert final["saved"] is False


@needs_chrome
def test_a_non_local_server_url_is_never_contacted(engine_runs: dict) -> None:
    assert engine_runs["remoteHost"]["calls"] == []
    assert engine_runs["localServer"] == [True, True, False, False, False]


@needs_chrome
def test_recording_posts_the_page_and_keeps_the_local_verdict(engine_runs: dict) -> None:
    run = engine_runs["recordLocal"]
    [call] = run["calls"]
    assert call["url"] == "http://127.0.0.1:9999/api/browsed"
    assert call["body"] == {
        "site": "linkedin",
        "job_id": "4012345678",
        "url": POSTING["url"],
        "title": POSTING["title"],
        "company": POSTING["company"],
        "location": POSTING["location"],
        "posted_ago": "2 days ago",
        "applicants": "Over 100 applicants",
        "text": POSTING["text"],
        "extraction": POSTING["extraction"],
    }
    final = run["final"]
    assert final["saved"] is True
    assert final["postingId"] == 7
    assert final["decision"]["source"] == "local"  # local mode: saved, not overridden


# --------------------------------------------------------------------------
# Service worker against a fake chrome
# --------------------------------------------------------------------------

FAKE_CHROME = """
function makeChrome(stored) {
  const session = {};
  const sync = { ...stored };
  const log = { badges: [], colors: [], tabMessages: [], listeners: 0 };
  const chrome = {
    runtime: {
      id: "test",
      getURL: (p) => "chrome-extension://test/" + p,
      onMessage: { addListener: () => { log.listeners++; } },
      sendMessage: async () => null,
    },
    storage: {
      sync: { get: async (defaults) => ({ ...defaults, ...sync }), set: async (v) => Object.assign(sync, v) },
      session: {
        get: async (k) => (k in session ? { [k]: session[k] } : {}),
        set: async (v) => Object.assign(session, v),
        remove: async (k) => { delete session[k]; },
      },
    },
    action: {
      setBadgeText: async (v) => log.badges.push(v),
      setBadgeBackgroundColor: async (v) => log.colors.push(v),
    },
    tabs: {
      sendMessage: async (tabId, m) => { log.tabMessages.push([tabId, m]); },
      onRemoved: { addListener: () => { log.listeners++; } },
    },
  };
  return { chrome, session, log };
}
"""


@needs_chrome
def test_service_worker_flow() -> None:
    out = run_js(
        [SRC / "labels.js", SRC / "classifier.js", SRC / "transport.js", SRC / "background.js"],
        {"RULES": rules_doc(), "POSTING": POSTING, "SERVER_DECISION": SERVER_DECISION},
        FAKE_CHROME
        + """
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        async function fetchFn(url, init) {
          if (url.endsWith("rules.json")) return { ok: true, json: async () => RULES };
          if (url.endsWith("/api/health")) return { ok: true, json: async () => ({ ok: true, classifier: "rules", rules_version: 3 }) };
          if (url.endsWith("/api/classify")) return { ok: true, status: 200, json: async () => ({ decision: SERVER_DECISION }) };
          throw new Error("unexpected " + url);
        }

        return (async () => {
          const results = {};

          // Local mode: the reply carries the verdict; the badge says CIT.
          let f = makeChrome({});
          let bg = JobFilterBackground.createBackground(f.chrome, fetchFn);
          bg.install();
          const sender = { tab: { id: 42 } };
          const reply = await bg.handle({ type: "posting", posting: POSTING, key: "k1" }, sender);
          const state = await bg.handle({ type: "get-state", tabId: 42 }, {});
          results.local = {
            label: reply.decision.label,
            badge: f.log.badges.at(-1),
            listeners: f.log.listeners,
            storedHasText: "text" in state.posting,
            chars: state.posting.chars,
            key: state.key,
          };
          await bg.handle({ type: "clear" }, sender);
          results.cleared = [await bg.handle({ type: "get-state", tabId: 42 }, {}), f.log.badges.at(-1)];

          // Server mode: local first, then an "update" pushed to the tab.
          f = makeChrome({ mode: "server" });
          bg = JobFilterBackground.createBackground(f.chrome, fetchFn);
          const first = await bg.handle({ type: "posting", posting: POSTING, key: "k2" }, sender);
          await sleep(50);
          const later = await bg.handle({ type: "get-state", tabId: 42 }, {});
          results.server = {
            first: [first.decision.source, first.pending],
            later: [later.state.decision.label, later.state.decision.source],
            pushed: f.log.tabMessages.map(([tab, m]) => [tab, m.type, m.key, m.state.decision.label]),
            badges: f.log.badges.map((b) => b.text),
          };

          results.health = await bg.handle({ type: "health" }, {});
          results.unknown = await bg.handle({ type: "nope" }, {});
          return results;
        })();
        """,
    )
    assert out["local"] == {
        "label": "RESTRICTED_CITIZEN",
        "badge": {"tabId": 42, "text": "CIT"},
        "listeners": 2,
        "storedHasText": False,  # session storage keeps a summary, not the text
        "chars": len(POSTING["text"]),
        "key": "k1",
    }
    assert out["cleared"] == [None, {"tabId": 42, "text": ""}]
    assert out["server"] == {
        "first": ["local", True],
        "later": ["RESTRICTED_US_PERSON", "server"],
        "pushed": [[42, "update", "k2", "RESTRICTED_US_PERSON"]],
        "badges": ["CIT", "USP"],
    }
    assert out["health"] == {"ok": True, "classifier": "rules", "rules_version": 3}
    assert out["unknown"] == {"error": "unknown message nope"}


# --------------------------------------------------------------------------
# Badge and content script
# --------------------------------------------------------------------------

@needs_chrome
def test_badge_renders_page_text_as_text_never_markup() -> None:
    out = run_js(
        [SRC / "labels.js", SRC / "transport.js", SRC / "badge.js"],
        {},
        """
        const badge = JobFilterBadge.mount(document);
        const evil = '<img src=x onerror="window.__pwned=1">Must be a U.S. citizen.';
        badge.show(
          { title: "<b>Engineer</b>", company: "Acme", location: "LA" },
          {
            decision: { label: "RESTRICTED_CITIZEN", evidence: evil, matchedRule: "r1", classifierName: "rules", source: "local" },
            local: { label: "RESTRICTED_CITIZEN" },
            server: { label: "OPEN" },
            serverError: null,
          },
          "k"
        );
        const root = badge.shadow;
        const shown = {
          images: root.querySelectorAll("img, b").length,
          quote: root.querySelector("blockquote").textContent,
          pill: root.querySelector(".pill .name").textContent,
          title: root.querySelector(".job").textContent,
          warnings: [...root.querySelectorAll(".warn")].map((n) => n.textContent),
          pwned: Boolean(window.__pwned),
        };
        // Dismissed stays dismissed for this posting, comes back for the next.
        root.querySelector(".close").click();
        const afterClose = root.querySelector(".pill");
        badge.pending({ title: "Next job" }, "k2");
        const nextJob = root.querySelector(".pill .name").textContent;
        badge.remove();
        return { shown, afterClose: afterClose === null, nextJob, removed: !document.getElementById(JobFilterBadge.HOST_ID) };
        """,
    )
    assert out["shown"]["images"] == 0
    assert out["shown"]["pwned"] is False
    assert out["shown"]["quote"].startswith("<img src=x")
    assert out["shown"]["pill"] == "U.S. citizens only"
    assert out["shown"]["title"] == "<b>Engineer</b>"
    assert out["shown"]["warnings"] == [
        "Local rules said U.S. citizens only; the server said No restriction found."
    ]
    assert out["afterClose"] is True
    assert out["nextJob"] == "Checking…"
    assert out["removed"] is True


@needs_chrome
def test_content_script_scans_and_shows_the_verdict() -> None:
    """content.js end to end, with a fake chrome standing in for the worker."""
    out = run_js(
        [SRC / "labels.js", SRC / "classifier.js", SRC / "extract.js", SRC / "transport.js", SRC / "badge.js"],
        {"RULES": rules_doc(), "CONTENT": (SRC / "content.js").read_text()},
        """
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        const engine = JobFilterEngine.create({ rules: RULES, fetch: null });
        const sent = [];
        globalThis.chrome = {
          runtime: {
            sendMessage: async (m) => {
              sent.push(m.type);
              return m.type === "posting" ? engine.run(m.posting, { mode: "local" }) : { ok: true };
            },
            onMessage: { addListener: (fn) => { globalThis.__listener = fn; } },
          },
        };

        document.body.insertAdjacentHTML("beforeend",
          '<main><h1>Test Pilot</h1><div class="job-description">' +
          '<p>Fly prototype aircraft from our desert test range every week of the year.</p>' +
          '<p>Write detailed reports after each flight for the engineering team to review.</p>' +
          '<p>This position requires a security clearance.</p></div></main>');

        // As the popup does: flag the explicit scan, then inject.
        globalThis.__jobfilterGeneric = true;
        new Function(CONTENT)();
        return (async () => {
          await sleep(50);
          const host = document.getElementById(JobFilterBadge.HOST_ID);
          const first = host.shadowRoot.querySelector(".pill .name").textContent;

          // Injecting again must not double up: it rescans the same badge.
          new Function(CONTENT)();
          await sleep(50);
          const hosts = document.querySelectorAll("#" + JobFilterBadge.HOST_ID).length;

          // The posting changes in place (an SPA): the observer picks it up.
          document.querySelector(".job-description p:last-child").textContent =
            "No security clearance required.";
          await sleep(1200);
          const after = document.getElementById(JobFilterBadge.HOST_ID)
            .shadowRoot.querySelector(".pill .name").textContent;
          return { first, hosts, after, sent };
        })();
        """,
    )
    assert out["first"] == "Security clearance required"
    assert out["hosts"] == 1
    assert out["after"] == "No restriction found"
    assert out["sent"].count("posting") == 3  # first scan, forced rescan, edit


@needs_chrome
def test_snapshot_is_static_and_names_its_source() -> None:
    """Popup → Download page HTML: scripts dropped (opened from disk they would
    re-render an empty page), JSON-LD kept, source URL recorded, live page untouched."""
    out = run_js(
        [SRC / "labels.js", SRC / "classifier.js", SRC / "extract.js", SRC / "transport.js", SRC / "badge.js"],
        {"CONTENT": (SRC / "content.js").read_text()},
        """
        globalThis.chrome = {
          runtime: {
            sendMessage: async () => ({ ok: true }),
            onMessage: { addListener: (fn) => { globalThis.__listener = fn; } },
          },
        };
        document.body.insertAdjacentHTML("beforeend",
          '<script type="application/ld+json">{"@type":"JobPosting","title":"Kept"}</' + 'script>' +
          '<main><p>Posting body</p></main>');
        new Function(CONTENT)();
        let snap = null;
        __listener({ type: "snapshot" }, null, (r) => { snap = r; });
        const parsed = new DOMParser().parseFromString(snap.html, "text/html");
        return {
          firstLine: snap.html.split("\\n")[0],
          href: location.href,
          url: snap.url,
          scripts: [...parsed.querySelectorAll("script")].map((s) => s.type),
          body: parsed.querySelector("main").textContent,
          liveScripts: document.querySelectorAll("script:not([type])").length,
        };
        """,
    )
    assert out["firstLine"] == f"<!-- jobfilter snapshot of {out['href']} -->"
    assert out["url"] == out["href"]
    assert out["scripts"] == ["application/ld+json"]
    assert out["body"] == "Posting body"
    assert out["liveScripts"] > 0, "the snapshot must copy the page, not strip the live one"


# --------------------------------------------------------------------------
# Manifest and packaging (no browser needed)
# --------------------------------------------------------------------------

def manifest() -> dict:
    return json.loads((EXT / "manifest.json").read_text())


def test_manifest_is_mv3_and_every_file_it_names_exists() -> None:
    m = manifest()
    assert m["manifest_version"] == 3
    named = [m["background"]["service_worker"], m["action"]["default_popup"]]
    for script in m["content_scripts"]:
        named += script["js"]
    for path in named:
        assert (EXT / path).is_file(), path


def test_content_scripts_load_dependencies_first() -> None:
    [script] = manifest()["content_scripts"]
    order = script["js"]
    assert order[-1] == "src/content.js"
    for before, after in [
        ("src/labels.js", "src/badge.js"),  # badge looks up label wording
        ("src/transport.js", "src/badge.js"),  # badge shows disagreements
        ("src/extract.js", "src/content.js"),
        ("src/badge.js", "src/content.js"),
    ]:
        assert order.index(before) < order.index(after), f"{before} must load before {after}"


def test_popup_injects_the_same_files_as_the_manifest() -> None:
    """"Scan this page" must load exactly what the automatic content script does."""
    [script] = manifest()["content_scripts"]
    popup = (EXT / "popup" / "popup.js").read_text()
    injected = re.search(r"files: \[(.*?)\]", popup, re.DOTALL).group(1)
    assert re.findall(r'"([^"]+)"', injected) == script["js"]


def test_service_worker_imports_exist() -> None:
    source = (SRC / "background.js").read_text()
    imported = re.search(r"importScripts\((.*?)\)", source).group(1)
    for name in re.findall(r'"([^"]+)"', imported):
        assert (SRC / name).is_file(), name


def test_popup_loads_only_bundled_scripts() -> None:
    """MV3 forbids remote code; keep it that way."""
    page = (EXT / "popup" / "popup.html").read_text()
    sources = re.findall(r'<script[^>]*src="([^"]+)"', page)
    assert sources
    for src in sources:
        assert not src.startswith(("http:", "https:", "//")), src
        assert (EXT / "popup" / src).resolve().is_file(), src
    assert "<script>" not in page  # inline scripts are blocked by MV3's CSP


def test_permissions_stay_narrow() -> None:
    m = manifest()
    assert set(m["permissions"]) == {"storage", "activeTab", "scripting"}
    assert m["host_permissions"] == ["http://127.0.0.1/*", "http://localhost/*"]
    [script] = m["content_scripts"]
    assert script["matches"] == [
        "https://www.linkedin.com/*",
        "https://*.indeed.com/*",
        "https://*.myworkdayjobs.com/*",
    ]


@needs_chrome
def test_every_extension_script_parses() -> None:
    """The popup and service worker are not executed above; at least compile them."""
    sources = {
        str(path): path.read_text()
        for path in sorted(EXT.rglob("*.js"))
    }
    errors = run_js(
        [],
        {"SOURCES": sources},
        """
        const errors = {};
        for (const [name, source] of Object.entries(SOURCES)) {
          // AsyncFunction: popup.js uses top-level await inside its IIFE.
          try { new (async function () {}).constructor(source); }
          catch (error) { errors[name] = String(error); }
        }
        return errors;
        """,
    )
    assert errors == {}
