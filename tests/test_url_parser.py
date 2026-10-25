"""Table-driven tests for jobfilter.url_parser. No network."""

from __future__ import annotations

import pytest

from jobfilter.models import ParsedUrl, Platform
from jobfilter.url_parser import WorkdayAddress, parse_url, workday_address

GRAYMATTER_URL = (
    "https://jobs.ashbyhq.com/graymatter-robotics/"
    "dea0f08d-ebf5-4e55-b9cb-e6302014f429"
)

# (case id, url, expected platform, expected slug, expected job_id)
SUPPORTED_CASES = [
    # --- ashby -----------------------------------------------------------
    ("ashby/plain", "https://jobs.ashbyhq.com/acme/abc-123", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/trailing-slash", "https://jobs.ashbyhq.com/acme/abc-123/", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/application", "https://jobs.ashbyhq.com/acme/abc-123/application", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/application-slash", "https://jobs.ashbyhq.com/acme/abc-123/application/", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/query", "https://jobs.ashbyhq.com/acme/abc-123?utm_source=x&ref=y", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/fragment", "https://jobs.ashbyhq.com/acme/abc-123#overview", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/query-and-application", "https://jobs.ashbyhq.com/acme/abc-123/application?utm_source=x", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/http", "http://jobs.ashbyhq.com/acme/abc-123", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/no-scheme", "jobs.ashbyhq.com/acme/abc-123", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/www", "https://www.jobs.ashbyhq.com/acme/abc-123", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/uppercase-host", "https://JOBS.ASHBYHQ.COM/acme/abc-123", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/port", "https://jobs.ashbyhq.com:443/acme/abc-123", Platform.ASHBY, "acme", "abc-123"),
    ("ashby/graymatter-ground-truth", GRAYMATTER_URL, Platform.ASHBY, "graymatter-robotics", "dea0f08d-ebf5-4e55-b9cb-e6302014f429"),
    # --- greenhouse ------------------------------------------------------
    ("gh/boards", "https://boards.greenhouse.io/acme/jobs/4567890", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/job-boards", "https://job-boards.greenhouse.io/acme/jobs/4567890", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/trailing-slash", "https://boards.greenhouse.io/acme/jobs/4567890/", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/application", "https://boards.greenhouse.io/acme/jobs/4567890/application", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/app-fragment", "https://boards.greenhouse.io/acme/jobs/4567890#app", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/query", "https://boards.greenhouse.io/acme/jobs/4567890?gh_src=abc123", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/no-scheme", "job-boards.greenhouse.io/acme/jobs/4567890", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/uppercase-jobs-segment", "https://boards.greenhouse.io/acme/JOBS/4567890", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/embed", "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=4567890", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/embed-boards-host", "https://boards.greenhouse.io/embed/job_app?token=4567890&for=acme", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/embed-real-nuro", "https://job-boards.greenhouse.io/embed/job_app?for=nuro&validityToken=ZTMhNlykXRAZhvYo9DLx%3D%3D&token=7351066", Platform.GREENHOUSE, "nuro", "7351066"),
    ("gh/board-gh-jid", "https://boards.greenhouse.io/acme?gh_jid=4567890", Platform.GREENHOUSE, "acme", "4567890"),
    ("gh/embed-no-for-slug-resolved-later", "https://boards.greenhouse.io/embed/job_app?token=4567890", Platform.GREENHOUSE, None, "4567890"),
    ("gh/embed-empty-for-slug-resolved-later", "https://boards.greenhouse.io/embed/job_app?for=&token=4567890", Platform.GREENHOUSE, None, "4567890"),
    ("gh/company-site-gh-jid", "https://careers.acme.com/jobs?gh_jid=4567890", Platform.GREENHOUSE, None, "4567890"),
    ("gh/real-nuro-company-site", "https://www.nuro.ai/careersitem?gh_jid=7351066", Platform.GREENHOUSE, None, "7351066"),
    ("gh/company-site-gh-jid-extra-params", "https://acme.com/careers/?gh_src=x&gh_jid=4567890#apply", Platform.GREENHOUSE, None, "4567890"),
    ("gh/real-noble-machines", "https://job-boards.greenhouse.io/undercontrolroboticsinc/jobs/4396196009", Platform.GREENHOUSE, "undercontrolroboticsinc", "4396196009"),
    ("ashby/company-site-ashby-jid-real-corvus", "https://www.corvus-robotics.com/careers?ashby_jid=52fbad75-35a9-4f22-a1bc-13c8d5ac633a#open-positions", Platform.ASHBY, None, "52fbad75-35a9-4f22-a1bc-13c8d5ac633a"),
    ("ashby/company-site-ashby-jid-uppercase", "https://acme.com/jobs?ashby_jid=52FBAD75-35A9-4F22-A1BC-13C8D5AC633A", Platform.ASHBY, None, "52fbad75-35a9-4f22-a1bc-13c8d5ac633a"),
    # --- lever -----------------------------------------------------------
    ("lever/plain", "https://jobs.lever.co/acme/9f2b1c3d-0000-4a5b-8c9d-112233445566", Platform.LEVER, "acme", "9f2b1c3d-0000-4a5b-8c9d-112233445566"),
    ("lever/trailing-slash", "https://jobs.lever.co/acme/abc-123/", Platform.LEVER, "acme", "abc-123"),
    ("lever/apply", "https://jobs.lever.co/acme/abc-123/apply", Platform.LEVER, "acme", "abc-123"),
    ("lever/query", "https://jobs.lever.co/acme/abc-123?lever-origin=applied", Platform.LEVER, "acme", "abc-123"),
    ("lever/no-scheme", "jobs.lever.co/acme/abc-123", Platform.LEVER, "acme", "abc-123"),
    ("lever/percent-encoded-slug", "https://jobs.lever.co/acme%20inc/abc-123", Platform.LEVER, "acme inc", "abc-123"),
    ("lever/real-rai", "https://jobs.lever.co/rai/dff73976-3e91-4897-af10-8d2433c63204", Platform.LEVER, "rai", "dff73976-3e91-4897-af10-8d2433c63204"),
    # --- dover: slug is the company name, slugified ------------------------
    ("dover/real-alloy", "https://app.dover.com/apply/Alloy%20Robotics/8c782ebb-49ae-4bc9-9491-924ba4d53ac7/?rs=76643084", Platform.DOVER, "alloy-robotics", "8c782ebb-49ae-4bc9-9491-924ba4d53ac7"),
    ("dover/no-trailing-slash", "https://app.dover.com/apply/Alloy%20Robotics/8C782EBB-49AE-4BC9-9491-924BA4D53AC7", Platform.DOVER, "alloy-robotics", "8c782ebb-49ae-4bc9-9491-924ba4d53ac7"),
    ("dover/punctuated-name", "https://app.dover.com/apply/Acme%2C%20Inc./8c782ebb-49ae-4bc9-9491-924ba4d53ac7", Platform.DOVER, "acme-inc", "8c782ebb-49ae-4bc9-9491-924ba4d53ac7"),
    # --- smartrecruiters: the id leads the last segment; the API address works too ----
    ("sr/real-with-title", "https://jobs.smartrecruiters.com/Intuitive/744000153260889-robotic-algorithm-and-controls-engineer-?oga=true", Platform.SMARTRECRUITERS, "intuitive", "744000153260889"),
    ("sr/id-only", "jobs.smartrecruiters.com/Intuitive/744000153260889", Platform.SMARTRECRUITERS, "intuitive", "744000153260889"),
    ("sr/api-job", "https://api.smartrecruiters.com/v1/companies/Intuitive/postings/744000153260889", Platform.SMARTRECRUITERS, "intuitive", "744000153260889"),
    ("sr/api-board-paged", "https://api.smartrecruiters.com/v1/companies/Intuitive/postings?offset=0&limit=100", Platform.SMARTRECRUITERS, "intuitive", None),
    ("sr/board", "https://jobs.smartrecruiters.com/Intuitive", Platform.SMARTRECRUITERS, "intuitive", None),
    ("sr/careers-board", "https://careers.smartrecruiters.com/Intuitive/", Platform.SMARTRECRUITERS, "intuitive", None),
    # --- workable: careers site (account + shortcode) and job board (view id, no account) ----
    ("workable/job", "https://apply.workable.com/octavian-robotics/j/3FF60B88A4/", Platform.WORKABLE, "octavian-robotics", "3FF60B88A4"),
    ("workable/job-apply-lowercase", "https://apply.workable.com/Octavian-Robotics/j/3ff60b88a4/apply/", Platform.WORKABLE, "octavian-robotics", "3FF60B88A4"),
    ("workable/short-link", "https://apply.workable.com/j/3FF60B88A4", Platform.WORKABLE, None, "3FF60B88A4"),
    ("workable/api-job", "https://apply.workable.com/api/v2/accounts/octavian-robotics/jobs/3FF60B88A4", Platform.WORKABLE, "octavian-robotics", "3FF60B88A4"),
    ("workable/board", "https://apply.workable.com/octavian-robotics/", Platform.WORKABLE, "octavian-robotics", None),
    ("workable/api-board", "https://apply.workable.com/api/v1/widget/accounts/octavian-robotics", Platform.WORKABLE, "octavian-robotics", None),
    ("workable/view", "https://jobs.workable.com/view/nPbcR1G5FfkYVf2cezffrL/full-stack-robotics-software-engineer-in-los-angeles-at-octavian-robotics?subid=direct", Platform.WORKABLE, None, "nPbcR1G5FfkYVf2cezffrL"),
    ("workable/view-no-title", "jobs.workable.com/view/nPbcR1G5FfkYVf2cezffrL", Platform.WORKABLE, None, "nPbcR1G5FfkYVf2cezffrL"),
    ("workable/company-page", "https://jobs.workable.com/company/4mLRJcLgYqAZkZBBoPcp9u/jobs-at-octavian-robotics", Platform.WORKABLE, "octavian-robotics", None),
    # --- eightfold: the employer's own host names the company -------------------
    ("eightfold/job", "https://careers.qualcomm.com/careers/job/446720880729", Platform.EIGHTFOLD, "qualcomm", "446720880729"),
    ("eightfold/job-apply", "https://apply.careers.microsoft.com/careers/job/1970393557037678/apply?utm_source=linkedin", Platform.EIGHTFOLD, "microsoft", "1970393557037678"),
    ("eightfold/job-any-host", "https://careers.example.co.uk/careers/job/123456789012", Platform.EIGHTFOLD, "example", "123456789012"),
    ("eightfold/share-link", "https://explore.jobs.netflix.net/careers?pid=790318858893&domain=netflix.com", Platform.EIGHTFOLD, "netflix", "790318858893"),
    ("eightfold/api-job", "https://careers.qualcomm.com/api/apply/v2/jobs/446720880729?domain=qualcomm.com", Platform.EIGHTFOLD, "qualcomm", "446720880729"),
    ("eightfold/api-position", "https://careers.qualcomm.com/api/pcsx/position_details?position_id=446720880729&domain=qualcomm.com", Platform.EIGHTFOLD, "qualcomm", "446720880729"),
    ("eightfold/shared-host", "https://app.eightfold.ai/careers/job/446720880729?domain=qualcomm.com", Platform.EIGHTFOLD, "qualcomm", "446720880729"),
    ("eightfold/tenant-host", "https://qualcomm.eightfold.ai/careers/job/446720880729", Platform.EIGHTFOLD, "qualcomm", "446720880729"),
    ("eightfold/api-search", "https://careers.qualcomm.com/api/apply/v2/jobs?domain=qualcomm.com&query=robotics&location=United%20States&start=0&num=10", Platform.EIGHTFOLD, "qualcomm", None),
    ("eightfold/pcsx-search", "https://careers.qualcomm.com/api/pcsx/search?domain=qualcomm.com&query=robotics", Platform.EIGHTFOLD, "qualcomm", None),
    ("eightfold/search-page", "https://careers.qualcomm.com/careers?query=robotics&location=United%20States", Platform.EIGHTFOLD, "qualcomm", None),
    ("eightfold/site", "https://explore.jobs.netflix.net/careers", Platform.EIGHTFOLD, "netflix", None),
    # --- amazon: one employer, so the slug is fixed ----------------------------
    ("amazon/real-with-tracking", "https://www.amazon.jobs/en/jobs/10568411/software-development-engineer-amazon-robotics?cmpid=SPLICX0248M&utm_source=linkedin.com&ss=paid", Platform.AMAZON, "amazon", "10568411"),
    ("amazon/no-title", "https://www.amazon.jobs/en/jobs/10568411", Platform.AMAZON, "amazon", "10568411"),
    ("amazon/no-locale", "amazon.jobs/jobs/10568411", Platform.AMAZON, "amazon", "10568411"),
    ("amazon/other-locale", "https://www.amazon.jobs/en-gb/jobs/10568411/some-title/", Platform.AMAZON, "amazon", "10568411"),
    ("amazon/apply-link", "https://account.amazon.jobs/jobs/10568411/apply", Platform.AMAZON, "amazon", "10568411"),
    ("amazon/team-page", "https://www.amazon.jobs/en/teams/amazon-robotics", Platform.AMAZON, "amazon-robotics", None),
    ("amazon/team-search", "https://www.amazon.jobs/en/search?base_query=&team_category%5B%5D=amazon-robotics&loc_query=", Platform.AMAZON, "amazon-robotics", None),
    ("amazon/team-without-prefix", "amazon.jobs/en/teams/project-kuiper", Platform.AMAZON, "amazon-project-kuiper", None),
    # --- rippling: a board URL has no job id ---------------------------------
    ("rippling/real-lyte", "https://ats.rippling.com/lyte/jobs/6d781383-02f4-47e5-bcf8-6d398590d476", Platform.RIPPLING, "lyte", "6d781383-02f4-47e5-bcf8-6d398590d476"),
    ("rippling/locale-apply-query", "https://ats.rippling.com/en-GB/lyte/jobs/6D781383-02F4-47E5-BCF8-6D398590D476/apply?jobSite=LinkedIn", Platform.RIPPLING, "lyte", "6d781383-02f4-47e5-bcf8-6d398590d476"),
    ("rippling/two-letter-slug-not-a-locale", "https://ats.rippling.com/hp/jobs/6d781383-02f4-47e5-bcf8-6d398590d476", Platform.RIPPLING, "hp", "6d781383-02f4-47e5-bcf8-6d398590d476"),
    ("rippling/board", "https://ats.rippling.com/lyte/jobs", Platform.RIPPLING, "lyte", None),
    ("rippling/board-bare", "https://ats.rippling.com/lyte", Platform.RIPPLING, "lyte", None),
    # --- workday: slug is the tenant; a board URL has no job id ----------
    ("workday/real-nvidia", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Robotics-Research-Engineer---Robot-Simulation-and-Evaluation_JR2023397", Platform.WORKDAY, "nvidia", "JR2023397"),
    ("workday/real-bd-no-locale", "https://bostondynamics.wd1.myworkdayjobs.com/Boston_Dynamics/job/Waltham-MA/Staff-Full-Stack-Software-Engineer_R2879", Platform.WORKDAY, "bostondynamics", "R2879"),
    ("workday/search-pane", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/details/Engineer_JR2014447?locationHierarchy1=2fcb", Platform.WORKDAY, "nvidia", "JR2014447"),
    ("workday/repost-suffix", "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-WI-Remote/Engineer_JR2021006-1", Platform.WORKDAY, "nvidia", "JR2021006-1"),
    ("workday/apply-flow", "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Engineer_R-123/apply/autofillWithResume?source=LinkedIn", Platform.WORKDAY, "acme", "R-123"),
    ("workday/percent-encoded", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Robotics-Engineer-%2D-Sim_JR2023397", Platform.WORKDAY, "nvidia", "JR2023397"),
    ("workday/no-wd-number", "https://acme.myworkdayjobs.com/careers/job/Engineer_R-123", Platform.WORKDAY, "acme", "R-123"),
    ("workday/board", "https://bostondynamics.wd1.myworkdayjobs.com/en-US/Boston_Dynamics", Platform.WORKDAY, "bostondynamics", None),
    ("workday/board-with-facets", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite?locationHierarchy1=2fcb&q=robotics", Platform.WORKDAY, "nvidia", None),
]

UNSUPPORTED_CASES = [
    ("empty", ""),
    ("whitespace-only", "   "),
    ("not-a-url", "not a url at all"),
    ("bare-word", "hello"),
    ("mailto", "mailto:jobs@acme.com"),
    ("ftp-scheme", "ftp://jobs.lever.co/acme/abc-123"),
    ("no-host", "https:///acme/abc-123"),
    ("unknown-host", "https://careers.acme.com/jobs/123"),
    ("linkedin", "https://www.linkedin.com/jobs/view/1234567890"),
    ("workday/host-only", "https://nvidia.wd5.myworkdayjobs.com/"),
    ("workday/locale-only", "https://nvidia.wd5.myworkdayjobs.com/en-US"),
    ("workday/sign-in-page", "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/userHome"),
    ("workday/job-without-id", "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Engineer"),
    ("workday/no-tenant", "https://myworkdayjobs.com/careers/job/Engineer_R-123"),
    ("workday/lookalike-host", "https://acme.myworkdayjobs.com.evil.com/careers/job/Engineer_R-123"),
    ("ashby/host-only", "https://jobs.ashbyhq.com"),
    ("ashby/board-only", "https://jobs.ashbyhq.com/acme"),
    ("ashby/too-deep", "https://jobs.ashbyhq.com/acme/abc-123/extra/deep"),
    ("ashby/apply-only", "https://jobs.ashbyhq.com/acme/application"),
    ("gh/board-only", "https://boards.greenhouse.io/acme"),
    ("gh/missing-jobs-segment", "https://boards.greenhouse.io/acme/4567890"),
    ("gh/wrong-middle-segment", "https://boards.greenhouse.io/acme/job/4567890"),
    ("gh/embed-non-numeric-token", "https://boards.greenhouse.io/embed/job_app?token=abc"),
    ("gh/company-site-non-numeric-gh-jid", "https://acme.com/careers?gh_jid=abc"),
    ("gh/company-site-empty-gh-jid", "https://acme.com/careers?gh_jid="),
    ("ashby/company-site-non-uuid-ashby-jid", "https://acme.com/careers?ashby_jid=123"),
    ("dover/careers-page", "https://app.dover.com/jobs/usealloy"),
    ("workable/host-only", "https://jobs.workable.com/"),
    ("workable/search", "https://jobs.workable.com/search?query=robotics"),
    ("workable/bad-shortcode", "https://apply.workable.com/octavian-robotics/j/not-a-code"),
    ("workable/view-without-id", "https://jobs.workable.com/view/"),
    ("workable/api-other", "https://apply.workable.com/api/v1/accounts"),
    ("sr/host-only", "https://jobs.smartrecruiters.com/"),
    ("sr/not-a-job", "https://jobs.smartrecruiters.com/Intuitive/not-a-job"),
    ("sr/api-other-resource", "https://api.smartrecruiters.com/v1/companies/Intuitive/departments"),
    ("eightfold/careers-on-unknown-host", "https://careers.acme.com/careers?query=robotics"),
    ("eightfold/short-job-id", "https://careers.acme.com/careers/job/12345"),
    ("eightfold/non-numeric-job", "https://careers.qualcomm.com/careers/job/software-engineer"),
    ("eightfold/bad-pid", "https://careers.qualcomm.com/careers?pid=abc"),
    ("eightfold/shared-host-no-company", "https://app.eightfold.ai/careers/job/446720880729"),
    ("eightfold/other-page", "https://careers.qualcomm.com/careers/home"),
    ("amazon/search-page","https://www.amazon.jobs/en/search?base_query=robotics"),
    ("amazon/teams-index", "https://www.amazon.jobs/en/teams/"),
    ("amazon/non-numeric-job", "https://www.amazon.jobs/en/jobs/abc"),
    ("rippling/host-only", "https://ats.rippling.com/"),
    ("rippling/non-uuid-job", "https://ats.rippling.com/lyte/jobs/123"),
    ("rippling/unknown-section", "https://ats.rippling.com/lyte/about/6d781383-02f4-47e5-bcf8-6d398590d476"),
    ("dover/non-uuid-job", "https://app.dover.com/apply/Alloy%20Robotics/123"),
    ("dover/no-company", "https://app.dover.com/apply/8c782ebb-49ae-4bc9-9491-924ba4d53ac7"),
    ("gh/embed-missing-token", "https://boards.greenhouse.io/embed/job_app?for=acme"),
    ("lever/board-only", "https://jobs.lever.co/acme"),
    ("lever/too-deep", "https://jobs.lever.co/acme/abc-123/extra/deep"),
    ("lever/eu-host-not-supported-yet", "https://jobs.eu.lever.co/acme/abc-123"),
    ("greenhouse-lookalike-host", "https://boards.greenhouse.io.evil.com/acme/jobs/123"),
]


@pytest.mark.parametrize(
    "url,platform,slug,job_id",
    [pytest.param(*c[1:], id=c[0]) for c in SUPPORTED_CASES],
)
def test_parses_supported_urls(
    url: str, platform: Platform, slug: str, job_id: str
) -> None:
    parsed = parse_url(url)
    assert parsed == ParsedUrl(
        platform=platform, slug=slug, job_id=job_id, url=url.strip()
    )
    assert parsed.supported


@pytest.mark.parametrize(
    "url", [pytest.param(c[1], id=c[0]) for c in UNSUPPORTED_CASES]
)
def test_rejects_unsupported_urls(url: str) -> None:
    parsed = parse_url(url)
    assert parsed.platform is Platform.UNSUPPORTED
    assert parsed.slug is None
    assert parsed.job_id is None
    assert not parsed.supported


@pytest.mark.parametrize(
    "url", [c[1] for c in SUPPORTED_CASES] + [c[1] for c in UNSUPPORTED_CASES]
)
def test_never_raises_and_keeps_original_url(url: str) -> None:
    """Even garbage input round-trips: we report it, we never drop or raise."""
    assert parse_url(url).url == url.strip()


def test_only_a_workday_board_url_is_a_board() -> None:
    assert parse_url("https://bostondynamics.wd1.myworkdayjobs.com/en-US/Boston_Dynamics").is_board
    assert not parse_url(GRAYMATTER_URL).is_board
    assert not parse_url("https://jobs.lever.co/acme").is_board  # unsupported


def test_workday_address_keeps_what_the_fetcher_needs() -> None:
    address = workday_address(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/details/"
        "US-CA-Santa-Clara/Robotics-Engineer-%2D-Sim_JR2023397/apply"
        "?locationHierarchy1=a&locationHierarchy1=b&q=robot+arm"
    )
    assert address == WorkdayAddress(
        host="nvidia.wd5.myworkdayjobs.com",
        tenant="nvidia",
        site="NVIDIAExternalCareerSite",
        # Still percent-encoded: it goes back into the API URL as it came.
        job_path="US-CA-Santa-Clara/Robotics-Engineer-%2D-Sim_JR2023397",
        job_id="JR2023397",
        # Every value of a repeated facet.
        query={"locationHierarchy1": ["a", "b"], "q": ["robot arm"]},
    )
    assert workday_address("https://jobs.lever.co/acme/abc") is None


def test_platform_compares_as_plain_string() -> None:
    """Fetchers and JSONL output rely on the StrEnum behaving like a str."""
    assert parse_url(GRAYMATTER_URL).platform == "ashby"
    assert str(Platform.UNSUPPORTED) == "unsupported"


def test_parsed_url_is_frozen_and_hashable() -> None:
    """Step 2 caches board responses keyed off these values."""
    parsed = parse_url(GRAYMATTER_URL)
    assert {parsed} == {parse_url(GRAYMATTER_URL)}
    with pytest.raises(AttributeError):
        parsed.slug = "other"  # type: ignore[misc]


def test_surrounding_whitespace_is_ignored() -> None:
    """urls.txt lines arrive with newlines attached."""
    assert parse_url(f"  {GRAYMATTER_URL}\n") == parse_url(GRAYMATTER_URL)
