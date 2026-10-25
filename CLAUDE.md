# Project: job-restriction-filter

## Goal
Given job posting URLs, fetch each posting's full description and classify whether it
restricts applicants by citizenship / US-person status / export control / security clearance.
Output the decision plus the exact sentence that triggered it.

We build in small, separately tested stages. Build ONLY what the current phase asks for.
After each step, stop, show me what you built and how to test it, and wait for my go-ahead.

## Architecture (design for this now, implement only Phase 1)
Keep these as separate modules with clear interfaces so later phases swap parts, not rewrite them:

1. `url_parser`  — URL → `ParsedUrl(platform, slug, job_id)`. Pure function, no network.
2. `fetchers/`   — one fetcher per platform, all implementing the same interface:
                   `fetch(parsed: ParsedUrl) -> Posting`. Phase 1: ashby, greenhouse, lever.
                   Phase 2 will add a browser-based fetcher behind the same interface.
3. `models`      — a normalized `Posting` dataclass: platform, company_slug, job_id, title,
                   location, url, description_text (plain text, HTML stripped), raw (original JSON).
4. `classifiers/` — interface: `classify(posting: Posting) -> Decision`.
                   Phase 1: `RuleClassifier` (regex). Later: Ollama, zero-shot (bart-large-mnli), Jev.
5. `Decision`    — label, confidence (1.0 for rules), evidence (matched sentence), matched_rule, classifier_name.
6. `cli`         — reads URLs from a text file or args, runs parse → fetch → classify,
                   writes results to JSONL and prints a readable summary table.

Labels:
- `RESTRICTED_CITIZEN`   — US citizens only
- `RESTRICTED_US_PERSON` — citizen / green card / refugee / asylee (ITAR/EAR language)
- `RESTRICTED_CLEARANCE` — security clearance required
- `INFO_NO_SPONSORSHIP`  — says no visa sponsorship (informational only, NOT a restriction)
- `UNCLEAR`              — some signal but ambiguous → needs human review
- `OPEN`                 — no restriction found

## Phase 1 — steps (stop after each)

### Step 1: URL parsing
Support these URL shapes:
- Ashby:      jobs.ashbyhq.com/{slug}/{job_id}
- Greenhouse: boards.greenhouse.io/{slug}/jobs/{job_id} and job-boards.greenhouse.io/{slug}/jobs/{job_id}
- Lever:      jobs.lever.co/{slug}/{job_id}
Unknown platforms return `platform="unsupported"` rather than raising.
Tests: pytest, table-driven, including query strings, trailing slashes, `/application` suffixes, bad URLs.

### Step 2: Fetchers (GET, no auth)
- Ashby: GET https://api.ashbyhq.com/posting-api/job-board/{slug}
  → returns ALL jobs for the company; select by `id == job_id`; use `descriptionPlain`.
  Cache the board response per slug so multiple URLs from one company = one request.
- Greenhouse: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}
  → `content` is HTML-escaped HTML; unescape then strip tags.
- Lever: GET https://api.lever.co/v0/postings/{slug}/{job_id}?mode=json
  → combine `descriptionPlain` and the text of the `lists` sections.
Requirements: timeout, one retry with backoff, clear error if job not found.
Save one real response per platform to `tests/fixtures/` and make ALL tests run offline
against fixtures. Network tests go behind a `@pytest.mark.live` marker.

### Step 3: Rule classifier
Keep all patterns in `rules.json` (not hardcoded in Python), so a future JS browser
extension can reuse the same rules file.
Must handle:
- Word boundaries: `\bITAR\b`; "itar" must NOT match inside "military".
- Negation: "no security clearance required" / "clearance not required" → not restricted.
- EEO boilerplate ("protected veteran status", "national origin") must not trigger anything.
- Distinguish "U.S. citizen" only vs "U.S. person / lawful permanent resident" wording.
- Split description into sentences; return the matched sentence as evidence.
- If multiple labels match, pick the most restrictive; if signals conflict, return UNCLEAR.

Ground-truth test case: GrayMatter Robotics (Ashby slug `graymatter-robotics`),
job `dea0f08d-ebf5-4e55-b9cb-e6302014f429`, has an "Export Control Requirements" section
listing US citizen/national, lawful permanent resident, refugee, asylee
→ expected `RESTRICTED_US_PERSON`.
Also write synthetic test strings for each label and each gotcha above.

### Step 4: CLI + output
`python -m jobfilter check urls.txt --out results.jsonl`
Print a summary table: company, title, label, evidence (truncated).
Also print a per-company summary: how many of that company's postings are restricted.

## Constraints
- Python 3.11+, `httpx`, `pytest`, standard library otherwise. No LLM calls in Phase 1.
- Type hints and small functions. No global state except the per-run fetch cache.
- Never silently drop postings; unclear cases stay in the output labeled UNCLEAR.
- README section per phase: how to run, how to test, what's next.

## Later phases (do NOT build yet; just keep the interfaces compatible)
- 2a: Browser fetcher (Playwright) for Workday / custom career pages with no public API.
- 2b: Model classifiers (Ollama, bart-large-mnli zero-shot, Jev) that only run on
      postings the rule classifier marks UNCLEAR; compare them against rule results.
- 2c: Browser extension for LinkedIn/Indeed reusing `rules.json`.

Start with Step 1. First show me the planned file layout, then implement Step 1 with tests.
