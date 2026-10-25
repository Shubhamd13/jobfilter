"""The server endpoints the browser extension calls.

/api/classify  -- the seam: the extension asks, the server's classifier
                  answers. Phase 2b swaps that classifier; the extension
                  does not change.
/api/browsed   -- record a posting seen on LinkedIn / Indeed / any page.
/api/health    -- the popup's "connected" line.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from jobfilter.models import Decision, Label, Posting
from jobfilter.storage import Store
from jobfilter.web import Api
from jobfilter.web.browsed import MAX_TEXT_CHARS, is_placeholder_company, url_id

# Fixtures and helpers shared with the web UI tests.
from .test_web import api, checked, live, post, store  # noqa: F401

LINKEDIN = {
    "site": "linkedin",
    "job_id": "4012345678",
    "url": "https://www.linkedin.com/jobs/view/4012345678/",
    "title": "Robotics Software Engineer",
    "company": "Acme Robotics, Inc.",
    "location": "Los Angeles, CA",
    "text": "We build robots.\nApplicants must be a U.S. person as defined by ITAR.",
    "extraction": {"description": "#job-details"},
}


# --------------------------------------------------------------------------
# /api/health
# --------------------------------------------------------------------------

def test_health_names_the_classifier(api: Api) -> None:
    response = api.handle("GET", "/api/health")
    assert response.status == 200
    assert response.payload["ok"] is True
    assert response.payload["classifier"] == "rules"
    assert isinstance(response.payload["rules_version"], int)


# --------------------------------------------------------------------------
# /api/classify
# --------------------------------------------------------------------------

def test_classify_returns_a_decision(api: Api) -> None:
    response = api.handle(
        "POST", "/api/classify", body={"text": "This position is open to U.S. citizens only."}
    )
    assert response.status == 200
    assert response.payload["decision"] == {
        "label": "RESTRICTED_CITIZEN",
        "confidence": 1.0,
        "evidence": "This position is open to U.S. citizens only.",
        "matched_rule": response.payload["decision"]["matched_rule"],
        "matched_rules": response.payload["decision"]["matched_rules"],
        "classifier_name": "rules",
    }
    assert response.payload["decision"]["matched_rule"]


def test_classify_stores_nothing(api: Api, store: Store) -> None:
    api.handle("POST", "/api/classify", body={"text": "Must be a U.S. citizen."})
    assert store.stats()["postings"] == 0


def test_classify_empty_text_is_unclear_not_an_error(api: Api) -> None:
    response = api.handle("POST", "/api/classify", body={"text": "   "})
    assert response.status == 200
    assert response.payload["decision"]["label"] == "UNCLEAR"


@pytest.mark.parametrize("body", [{}, {"text": None}, {"text": 12}, {"text": ["a"]}])
def test_classify_needs_text(api: Api, body: dict) -> None:
    response = api.handle("POST", "/api/classify", body=body)
    assert response.status == 400


class FixedClassifier:
    """Stands in for a Phase 2b model classifier."""

    name = "ollama-test"

    def __init__(self) -> None:
        self.seen: list[Posting] = []

    def classify(self, posting: Posting) -> Decision:
        self.seen.append(posting)
        return Decision(
            label=Label.UNCLEAR,
            confidence=0.42,
            evidence=None,
            matched_rule=None,
            classifier_name=self.name,
        )


def test_classify_uses_whatever_classifier_the_server_has(store: Store) -> None:
    """The seam the extension relies on: swap the classifier, same endpoint."""
    model = FixedClassifier()
    api = Api(store, checker=lambda urls, expand=False: [], classifier=model)

    response = api.handle(
        "POST", "/api/classify", body={"text": "Some text.", "title": "Engineer"}
    )
    assert response.payload["decision"]["classifier_name"] == "ollama-test"
    assert response.payload["decision"]["confidence"] == 0.42
    assert model.seen[0].description_text == "Some text."
    assert model.seen[0].title == "Engineer"  # a model may want the title too
    assert api.handle("GET", "/api/health").payload["classifier"] == "ollama-test"


# --------------------------------------------------------------------------
# /api/browsed
# --------------------------------------------------------------------------

def test_browsed_posting_is_recorded(api: Api, store: Store) -> None:
    response = api.handle("POST", "/api/browsed", body=LINKEDIN)
    assert response.status == 200
    assert response.payload["decision"]["label"] == "RESTRICTED_US_PERSON"

    [row] = store.postings()
    assert row["posting_id"] == response.payload["posting_id"]
    assert row["platform"] == "linkedin"
    assert row["company_slug"] == "acme-robotics-inc"
    assert row["company_name"] == "Acme Robotics, Inc."
    assert row["job_id"] == "4012345678"
    assert row["origin"] == "browsed"
    assert row["label"] == "RESTRICTED_US_PERSON"
    assert row["title"] == "Robotics Software Engineer"


def test_listing_signals_are_stored_and_survive_a_read_that_missed_them(
    api: Api, store: Store
) -> None:
    listed = {**LINKEDIN, "posted_ago": "Reposted 2 weeks ago", "applicants": "Over 100 applicants"}
    api.handle("POST", "/api/browsed", body=listed)
    [row] = store.postings()
    assert row["posted_ago"] == "Reposted 2 weeks ago"
    assert row["applicants"] == "Over 100 applicants"
    seen_at = row["listing_seen_at"]
    assert seen_at

    # A later read whose selectors missed the line keeps what we had.
    api.handle("POST", "/api/browsed", body=LINKEDIN)
    [row] = store.postings()
    assert (row["posted_ago"], row["applicants"], row["listing_seen_at"]) == (
        "Reposted 2 weeks ago", "Over 100 applicants", seen_at,
    )

    # A fresh read replaces both together, even a part it no longer shows.
    api.handle("POST", "/api/browsed", body={**LINKEDIN, "posted_ago": "3 weeks ago"})
    [row] = store.postings()
    assert (row["posted_ago"], row["applicants"]) == ("3 weeks ago", None)


def test_the_server_classifies_rather_than_trusting_the_page(api: Api) -> None:
    """A body claiming its own verdict is ignored: the text decides."""
    body = {**LINKEDIN, "decision": {"label": "OPEN"}, "label": "OPEN"}
    response = api.handle("POST", "/api/browsed", body=body)
    assert response.payload["decision"]["label"] == "RESTRICTED_US_PERSON"


def test_viewing_a_posting_again_does_not_duplicate_it(api: Api, store: Store) -> None:
    first = api.handle("POST", "/api/browsed", body=LINKEDIN)
    second = api.handle("POST", "/api/browsed", body=LINKEDIN)
    assert first.payload["posting_id"] == second.payload["posting_id"]
    assert store.stats()["postings"] == 1
    assert len(store.history(first.payload["posting_id"])) == 1  # verdict unchanged


def test_an_edited_posting_adds_history(api: Api, store: Store) -> None:
    first = api.handle("POST", "/api/browsed", body=LINKEDIN)
    api.handle("POST", "/api/browsed", body={**LINKEDIN, "text": "We build robots."})
    history = store.history(first.payload["posting_id"])  # oldest first
    assert [h["label"] for h in history] == ["RESTRICTED_US_PERSON", "OPEN"]


def test_a_generic_page_is_grouped_by_host_and_keyed_by_url(
    api: Api, store: Store
) -> None:
    page = {
        "site": "web",
        "url": "https://careers.example.com/jobs/firmware?utm_source=x",
        "title": "Firmware Engineer",
        "text": "No security clearance required.",
    }
    first = api.handle("POST", "/api/browsed", body=page)
    # Same page, different tracking query: same posting.
    again = api.handle(
        "POST", "/api/browsed", body={**page, "url": "https://careers.example.com/jobs/firmware/"}
    )
    assert first.status == again.status == 200
    assert first.payload["posting_id"] == again.payload["posting_id"]

    [row] = store.postings()
    assert row["platform"] == "web"
    assert row["company_slug"] == "careers.example.com"
    assert row["job_id"] == url_id(page["url"])
    assert row["label"] == "OPEN"


def test_a_workday_posting_belongs_to_the_tenant_in_its_host(
    api: Api, store: Store
) -> None:
    body = {
        "site": "workday",
        "job_id": "JR2023397",
        "url": "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Robotics-Research-Engineer_JR2023397",
        "title": "Robotics Research Engineer",
        # Page text claims a name; the host decides, since req ids are per tenant.
        "company": "2100 NVIDIA USA",
        "text": "Applicants must be U.S. persons as defined by ITAR.",
    }
    response = api.handle("POST", "/api/browsed", body=body)
    assert response.status == 200

    [row] = store.postings()
    assert row["platform"] == "workday"
    assert row["company_slug"] == "nvidia"
    assert row["job_id"] == "JR2023397"
    assert row["label"] == "RESTRICTED_US_PERSON"

    missing_id = api.handle("POST", "/api/browsed", body={**body, "job_id": None})
    assert missing_id.status == 400


def test_indeed_posting_is_recorded(api: Api, store: Store) -> None:
    body = {
        "site": "indeed",
        "job_id": "a1b2c3d4e5f60718",
        "url": "https://www.indeed.com/viewjob?jk=a1b2c3d4e5f60718",
        "title": "Embedded Engineer",
        "company": "Saguaro Systems",
        "text": "This position is open to U.S. citizens only.",
    }
    response = api.handle("POST", "/api/browsed", body=body)
    assert response.payload["decision"]["label"] == "RESTRICTED_CITIZEN"
    assert store.postings()[0]["platform"] == "indeed"


# --- company identity: a name read off a page is not an identity ----------

def _linkedin_job(job_id: str, company: str | None, text: str = "We build robots.") -> dict:
    body = {
        **LINKEDIN,
        "job_id": job_id,
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "text": text,
    }
    if company is None:
        body.pop("company")
    else:
        body["company"] = company
    return body


def test_two_stealth_startups_are_not_one_company(api: Api, store: Store) -> None:
    api.handle("POST", "/api/browsed", body=_linkedin_job(
        "4400000001", "Stealth Startup", "Applicants must be a U.S. person as defined by ITAR."))
    api.handle("POST", "/api/browsed", body=_linkedin_job("4400000002", "Stealth Startup"))

    summary = store.company_summary()
    assert sorted(c["company_slug"] for c in summary) == [
        "stealth-startup~4400000001",
        "stealth-startup~4400000002",
    ]
    # One restricted stealth employer must not taint the other.
    assert sorted(c["restricted"] for c in summary) == [0, 1]
    assert {r["company_name"] for r in store.postings()} == {"Stealth Startup"}


def test_postings_with_no_company_name_are_not_pooled(api: Api, store: Store) -> None:
    api.handle("POST", "/api/browsed", body=_linkedin_job("4400000003", None))
    api.handle("POST", "/api/browsed", body=_linkedin_job("4400000004", None))
    assert sorted(c["company_slug"] for c in store.company_summary()) == [
        "unknown~4400000003",
        "unknown~4400000004",
    ]


def test_real_company_names_still_group(api: Api, store: Store) -> None:
    api.handle("POST", "/api/browsed", body=_linkedin_job("4400000005", "Acme Robotics"))
    api.handle("POST", "/api/browsed", body=_linkedin_job("4400000006", "Acme Robotics"))
    [company] = store.company_summary()
    assert company["company_slug"] == "acme-robotics"
    assert company["total"] == 2


def test_a_job_is_one_row_even_if_its_company_name_was_missed(
    api: Api, store: Store
) -> None:
    """LinkedIn job ids are site-wide: the name read off the page is not part of the identity."""
    missed = api.handle("POST", "/api/browsed", body=_linkedin_job("4400000007", None))
    named = api.handle("POST", "/api/browsed", body=_linkedin_job("4400000007", "Acme Robotics"))
    missed_again = api.handle("POST", "/api/browsed", body=_linkedin_job("4400000007", None))

    assert missed.payload["posting_id"] == named.payload["posting_id"] == missed_again.payload["posting_id"]
    [row] = store.postings()
    # The real name wins and a later missed read does not erase it.
    assert row["company_slug"] == "acme-robotics"
    assert row["company_name"] == "Acme Robotics"


@pytest.mark.parametrize(
    "name",
    [
        "Stealth Startup", "Stealth", "Stealth Mode", "Stealth AI Startup", "stealth-mode company",
        "Confidential", "Confidential Company", "Company Confidential", "Undisclosed",
        "Private Company", "Anonymous Employer", "Unknown", "N/A", "", None,
    ],
)
def test_placeholder_company_names(name: str | None) -> None:
    assert is_placeholder_company(name)


@pytest.mark.parametrize(
    "name",
    ["Acme Robotics", "Stealthy Robotics", "Confidence Labs", "Private Equity Partners", "Unknown Worlds Entertainment"],
)
def test_real_names_are_not_placeholders(name: str) -> None:
    assert not is_placeholder_company(name)


@pytest.mark.parametrize(
    "change, error",
    [
        pytest.param({"site": "monster"}, "site must be one of", id="unknown-site"),
        pytest.param({"site": None}, "site must be one of", id="no-site"),
        pytest.param({"url": "javascript:alert(1)"}, "absolute http(s) URL", id="js-url"),
        pytest.param({"url": "/jobs/view/1"}, "absolute http(s) URL", id="relative-url"),
        pytest.param({"text": None}, "text must be a string", id="no-text"),
        pytest.param({"job_id": None}, "job_id is required", id="linkedin-without-id"),
        pytest.param({"title": {"x": 1}}, "title must be a string", id="object-title"),
        pytest.param({"company": "x" * 501}, "longer than", id="huge-company"),
        pytest.param({"text": "x" * (MAX_TEXT_CHARS + 1)}, "longer than", id="huge-text"),
    ],
)
def test_bad_browsed_bodies_are_400s(
    api: Api, store: Store, change: dict, error: str
) -> None:
    response = api.handle("POST", "/api/browsed", body={**LINKEDIN, **change})
    assert response.status == 400
    assert error in response.payload["error"]
    assert store.stats()["postings"] == 0


# --------------------------------------------------------------------------
# Over HTTP: what the service worker actually does
# --------------------------------------------------------------------------

def test_classify_over_http(live: str) -> None:
    status, payload = post(live, "/api/classify", {"text": "An active TS/SCI clearance is required."})
    assert status == 200
    assert payload["decision"]["label"] == "RESTRICTED_CLEARANCE"


def test_web_pages_cannot_read_the_api(live: str) -> None:
    """No CORS headers, no preflight answer.

    The extension's service worker reaches the server through its
    host_permissions, which bypass CORS. An ordinary web page cannot: its
    JSON POST needs a preflight this server never grants, so a site you visit
    cannot query or write your database.
    """
    request = urllib.request.Request(
        live + "/api/health", headers={"Origin": "https://evil.example"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.headers.get("Access-Control-Allow-Origin") is None

    preflight = urllib.request.Request(
        live + "/api/browsed",
        method="OPTIONS",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(preflight, timeout=5)
    assert exc.value.headers.get("Access-Control-Allow-Origin") is None
