# Changing and extending jobfilter

A guide for someone new to this codebase. Each section is one kind of change, from the
easiest (editing a settings file) to the most involved (adding a job site). You do not need
to read it in order: find your change in the table and jump to it.

| I want to… | What to edit | Code needed? |
|---|---|---|
| Change which jobs I see (locations, seniority, years, kinds of work) | `profile.json` | No |
| Fix a wrong restriction label (citizenship, clearance, sponsorship) | `rules.json` | No, but regex |
| Fix a wrong title cut, or what counts as "robotics" | `role_rules.json` | No, but regex |
| Read a location the tool does not understand | `jobfilter/location.py` | A line or two |
| Change what the model reads, or try another model | `jobfilter/roles/reader.py` | Some Python |
| Add a job site (Amazon is the worked example) | `jobfilter/fetchers/` | Python |
| Add a filter or a page | `storage/`, `web/` | Python and JavaScript |
| Store something new | `storage/schema.sql` | SQL and Python |

## How a posting moves through the tool

Every change lands on one of these steps. Each step is its own module with a small
interface, so you can change one without touching the others.

```
a URL
  │  url_parser.py          which site, which company, which job?      -> ParsedUrl
  ▼
fetchers/<site>.py          download the posting                        -> Posting
  ▼
classifiers/rules.py        may I apply? (rules.json)                   -> a label + the sentence
  ▼
storage/store.py            saved in jobfilter.db
  ▼
roles/funnel.py             is it my kind of job? (role_rules.json + profile.json)
  ▼
roles/reader.py             a local model reads what the rules left     -> facts -> fit / stretch / skip
  ▼
web/                        the page: Inbox, Explore, Companies, Add jobs
```

Two ideas run through all of it:

- **Rules and preferences are separate files.** `rules.json` and `role_rules.json` say what
  a posting *means*. `profile.json` says what *you* want. To suit someone else, swap the
  profile, not the rules.
- **Every decision quotes its reason.** A label carries the sentence that caused it; a
  title cut carries the words; a model fact carries the sentence it came from. If you add
  a decision, keep that.

## Before you change anything

```bash
make venv install            # once: a Python 3.12 virtualenv with the project installed
make test                    # the whole suite, offline, about 40 seconds
```

Commands below use `.venv/bin/python -E`, which works whether or not the virtualenv is
activated. The `-E` makes Python ignore `PYTHONPATH`, which matters when another
installation (ROS, for one) puts its packages there; see
[USAGE.md](../USAGE.md#before-you-start).

For how each part decides what it decides, read [HOW-IT-WORKS.md](HOW-IT-WORKS.md) first;
for what each job site returns, [SITES.md](SITES.md).

There is no model call, network call or real database in the tests. If you add one, put it
behind `@pytest.mark.live` like the existing ones.

---

## 1. Change your preferences: `profile.json`

No code. Copy the example once, then edit your copy (it is git-ignored, so it stays yours):

```bash
cp profile.example.json profile.json
```

| Field | What it does | Example change |
|---|---|---|
| `locations` | Which places to keep: `us`, `remote`, `unknown`, `outside_us` | Add `"outside_us"` to see jobs abroad |
| `labels` | Which restriction labels to keep | Remove `"INFO_NO_SPONSORSHIP"` to drop no-sponsorship jobs |
| `skip_categories` | Title categories to cut (names come from `role_rules.json`) | Add `"senior"` to cut senior titles by rule; remove `"internship"` to keep interns |
| `focus` | Topics a posting must be about | `[]` keeps every engineering role, not only robotics |
| `fit.want` | Kinds of work that are a **fit** | Add `"embedded_firmware"` |
| `fit.stretch` | Kinds of work that are a **stretch** | Remove `"technician_ops"` to skip technician roles |
| `fit.max_years` / `fit.stretch_years` | Years asked: up to `max_years` is a fit, up to `stretch_years` a stretch, more is a skip | `2` and `4` for a new graduate |
| `fit.highest_degree` | Your degree: `bachelors`, `masters` or `phd` | A posting requiring more is a skip |
| `skills` | Your own tools and topics, each a name, a pattern and a weight; a posting's match score is the sum of the weights it mentions | Add `{"name": "Rust", "pattern": "\\brust\\b", "weight": 2}` |
| `best_fit_score` | A **fit** scoring at least this becomes a **best fit**, listed first in the Inbox | Lower it to see more best fits |

The kinds of work (`robotics_software`, `ml_ai`, `chip_design`, …) are listed in `CORE_WORK`
in [reader.py](../jobfilter/roles/reader.py).

**Apply it.** Press **Re-screen** on the Explore page, or:

```bash
.venv/bin/python -E -m jobfilter screen
```

That takes seconds and calls no model: the model's readings are stored, and only the
verdict is recomputed.

**Check it.** `python -m jobfilter.roles` prints how many postings stop at each stage, and
`--show <stage>` lists examples with the reason:

```bash
.venv/bin/python -E -m jobfilter.roles
.venv/bin/python -E -m jobfilter.roles --show seniority -n 40
```

---

## 2. Fix a restriction label: `rules.json`

This file decides `OPEN`, `UNCLEAR`, `INFO_NO_SPONSORSHIP` and the three `RESTRICTED_*`
labels. It is plain JSON holding regular expressions; the Python and the browser
extension both read it.

A rule looks like this:

```json
{
  "id": "no-sponsorship",
  "pattern": "\\bno\\s+(?:visa\\s+|work\\s+|immigration\\s+)?sponsorship\\b",
  "label": "INFO_NO_SPONSORSHIP",
  "note": "'no visa sponsorship'."
}
```

- `pattern` is matched against one sentence at a time, ignoring case.
- `unless` (optional) is a second pattern: if it also matches the sentence, the rule does
  not fire. That is how "clearance **preferred**" is kept from reading as a requirement.
- Besides `rules`, the file has `negations` ("no clearance required" cancels a clearance
  rule in the same sentence), `open_signals` (a contradiction anywhere makes the posting
  `UNCLEAR`) and `exclusions` (EEO boilerplate, ignored). Each list has a `_note` in the
  file explaining it.

### The steps

Say "must hold a green card" should be `RESTRICTED_US_PERSON` and is not.

1. **Write the case first**, in `tests/cases.json`. This file is the contract: every
   wording that matters has a line here, and both engines must agree on all of them.

   ```json
   { "id": "usperson/must-hold-green-card", "text": "Applicants must hold a green card.", "label": "RESTRICTED_US_PERSON" }
   ```

2. **Run the rule tests and watch it fail**:

   ```bash
   .venv/bin/python -E -m pytest tests/test_rule_classifier.py -q
   ```

3. **Add or edit the rule** in `rules.json`, and raise `"version"` by one.
4. **Copy it to the extension** (a test checks the two files are identical):

   ```bash
   make sync-rules
   ```

5. **Run the tests again**, including the JavaScript engine:

   ```bash
   .venv/bin/python -E -m pytest tests/test_rule_classifier.py tests/test_js_parity.py -q
   ```

6. **See what it does to real postings before committing to it.** The dry run lists every
   label that would change and writes nothing:

   ```bash
   .venv/bin/python -E -m jobfilter reclassify --dry-run
   ```

   If hundreds move and you expected ten, the pattern is too wide.

7. **Apply it**, then re-place the postings:

   ```bash
   .venv/bin/python -E -m jobfilter reclassify
   .venv/bin/python -E -m jobfilter screen
   ```

### Regex habits that save you

- Put `\\b` (a word boundary) at both ends. Without it `itar` matches inside "military".
- In JSON every backslash is doubled: `\\b`, `\\s+`.
- Use only syntax both Python and JavaScript accept: no named groups, no lookbehind of
  varying length. The note at the top of `rules.json` lists what is safe.
- Prefer a narrow rule plus a second narrow rule over one clever wide one.
- When a rule is simply wrong for everyone, fix the rule. Do not add a profile switch to
  work around it.

---

## 3. Fix a title cut or the "robotics" test: `role_rules.json`

This file decides what a job **title** makes certain: "Technical Recruiter" is HR,
"Staff Engineer" is staff-level, "PCB Layout Engineer" is electrical. Anything a title
leaves in doubt is passed on to the model.

```json
{"id": "hr-recruiting", "category": "hr_recruiting", "unless_set": "engineering_title",
 "pattern": "\\b(recruit(er|ers|ing|ment)|talent (acquisition|partner)|human resources|hr)\\b"}
```

Three things to know:

- **A title is read in parts.** `Senior Thermal Engineer, Expeditionary AI` is the role
  `Senior Thermal Engineer` and the team `Expeditionary AI`.
- **`unless_set` names a shared exception**, checked against the matching part and the
  role part. `engineering_title` is why `AI Solutions Engineer, Talent Acquisition` is not
  HR; `software_title` is why `Software Engineer, Manufacturing Test` is not manufacturing.
- **`"where": "role"`** makes a rule look at the role part only.

The `focus` section decides what counts as being "about robotics": a title term, or at
least `min_sentences` separate sentences of the description mentioning a term, or a company
where most postings qualify.

### The steps

1. Add a real title to the table in `tests/test_roles.py` with the category you expect
   (or the one it must *not* get: there is a table for that too).
2. Edit `role_rules.json`. To add a whole new category, add it under `categories` with
   its group (`non_engineering`, `seniority` or `mech_elec`), then a rule for it, then
   list it in `skip_categories` in the profile if it should be cut.
3. Test, then look at the effect on real titles:

   ```bash
   .venv/bin/python -E -m pytest tests/test_roles.py -q
   .venv/bin/python -E -m jobfilter.roles --show non_engineering -n 50
   .venv/bin/python -E -m jobfilter.roles --show model -n 50      # what still reaches the model
   ```

4. `jobfilter screen` (or **Re-screen**) to store it.

---

## 4. Teach it a location

`VNM Ho Chi Minh` once counted as "unknown" and slipped into a US-only list. Locations are
read by [location.py](../jobfilter/location.py), which is lists of names: countries,
country codes, non-US cities, US states and US cities.

1. Add the string to the table in `tests/test_location.py` with `US`, `OUT` or `UNKNOWN`.
2. Add the missing name to the right list in `location.py`. A US city is probably already
   in `jobfilter/us_cities.tsv` (every one of 1,000+ people) but only counts there as a
   whole comma-separated part; add it to `US_CITIES` to have it found inside a longer
   string ("Seattle HQ"). Regenerate the table with `scripts/build_us_cities.py` (its
   docstring has the download commands); edit its `NOT_CITIES` to drop a name.
3. `.venv/bin/python -E -m pytest tests/test_location.py -q`, then `jobfilter screen`.

The order of the checks matters and is written at the top of the file: a US state beats a
foreign city of the same name, which is why "Melbourne, Florida" is US.

---

## 5. Change what the model reads

[reader.py](../jobfilter/roles/reader.py) holds the prompt, the reply's shape, the checks
on the reply, and `judge`, which turns facts into a verdict.

- **Try another model**: no code. Pull it and name it.

  ```bash
  ollama pull <model>
  .venv/bin/python -E -m jobfilter read --model <model> --limit 25
  ```

  Postings already read by another model are read again by the new one.

- **Add a kind of work**: add it to `CORE_WORK`, describe it in `INSTRUCTIONS` (one line,
  with examples of what belongs there), raise `PROMPT_VERSION`, and add it to `fit.want`
  or `fit.stretch` in the profile if it should not be skipped.

- **Change the prompt**: edit `INSTRUCTIONS` and raise `PROMPT_VERSION`. Stored readings
  from the old prompt then count as unread, and `jobfilter read` redoes them.

- **Change the verdict**: edit `judge`. It is a pure function with a table of cases in
  `tests/test_reader.py`; add your case there first.

Two rules this file follows, worth keeping:

- **The model reports facts; code decides.** Asked "should this person apply?", a small
  model was unreliable. Asked what the posting says, with a quote, it is checkable.
- **A fact that cannot be checked never helps a posting pass.** A quote must be in the
  posting; a number must be in its quote. When that fails the verdict is "needs a look".

Test without a model: `tests/test_reader.py` feeds canned replies through an
`httpx.MockTransport`. `pytest -m live tests/test_reader.py` runs one real reading.

---

## 6. Add a job site: Amazon Jobs, step by step

A *fetcher* turns a job URL into a `Posting`. Adding one touches four files and adds two.
This is exactly how [amazon.py](../jobfilter/fetchers/amazon.py) was added.

### Step 1: find where the site keeps its data

Open a job page with the browser's developer tools on the **Network** tab, filter by
`Fetch/XHR`, and reload. You are looking for a request that returns JSON with the job's
description in it. Then reproduce it without a browser:

```bash
curl -s 'https://www.amazon.jobs/en/search.json?base_query=10568411&result_limit=10' | head -c 600
```

For Amazon, the job page itself has no JSON (adding `.json` answers 406), but the site's
search does, and searching for a job's number returns that job. Note what you learn: which
field is the id (`id_icims`), where the text is (`description`, `basic_qualifications`,
`preferred_qualifications`), and what a missing job looks like (`"jobs": []`).

If a site only serves HTML behind JavaScript, with no JSON anywhere, it needs a browser
fetcher, which this project does not have yet.

### Step 2: name the platform

In [models.py](../jobfilter/models.py), add it to `Platform`:

```python
AMAZON = "amazon"
```

### Step 3: recognize its URLs

In [url_parser.py](../jobfilter/url_parser.py): add the host to `_HOSTS`, write a small
function that pulls the company slug and job id out of the path, and add it to
`_PATH_PARSERS`.

```python
def _parse_amazon(segments, query):
    # amazon.jobs/[{locale}/]jobs/{job_id}[/{title-slug}]
    if segments and _LOCALE_RE.fullmatch(segments[0]):
        segments = segments[1:]
    if len(segments) in (2, 3) and segments[0].lower() == "jobs" and _AMAZON_JOB_ID_RE.fullmatch(segments[1]):
        return "amazon", segments[1]
    return None
```

Return `None` for anything that is not a job (a search page, a team page): the URL is then
reported as unsupported instead of failing later.

Some links do not say which company they belong to (a Workable job-board link, a short
link). Return the job id with a slug of `None` and let the fetcher find the company:
[workable.py](../jobfilter/fetchers/workable.py) shows how, and how to make two links to
the same job end up as one stored posting.

Add rows to the two tables in `tests/test_url_parser.py`: URLs that must parse (with a
tracking query, without a title, the apply link) and URLs that must not. Then add the
shape to the table in [SITES.md](SITES.md#links-what-is-accepted), and any link that looks
supported but is not to [its "do not work" list](SITES.md#links-that-do-not-work-and-what-to-do).

### Step 4: write the fetcher

A fetcher is a class with a `platform` and one method. The smallest useful shape:

```python
class AmazonFetcher:
    platform: ClassVar[Platform] = Platform.AMAZON

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def fetch(self, parsed: ParsedUrl) -> Posting:
        found = self._client.get_json(SEARCH.format(job_id=parsed.job_id), platform=self.platform)
        job = next((j for j in (found or {}).get("jobs", []) if str(j.get("id_icims")) == parsed.job_id), None)
        if job is None:
            raise JobNotFound(f"Amazon Jobs has no job {parsed.job_id!r}", url=parsed.url, platform=self.platform)
        return Posting(
            platform=Platform.AMAZON,
            company_slug=team_slug(job["team"]["label"]),   # "amazon-robotics"
            job_id=parsed.job_id,
            title=job.get("title") or "",
            location=...,                 # "Westboro, MA, USA"
            url=...,                      # the job's own URL, without tracking parameters
            description_text=...,         # plain text: use html_to_text and join_sections
            raw=job,                      # the original JSON, kept for later
        )
```

What to get right:

- **Use the shared `HttpClient`.** It has the timeout and the one retry, and tests replace
  its transport. Do not call `httpx` or `requests` directly.
- **`description_text` is plain text.** `html_to_text` strips tags; `join_sections` joins
  parts with blank lines. Keep headings such as "Preferred Qualifications": the model uses
  them to tell required from preferred.
- **Raise the typed errors** from `fetchers/base.py`: `JobNotFound`, `BoardNotFound`,
  `TransportFailure`. They become a visible row under "Could not be checked"; nothing is
  dropped silently.
- **Check the answer is the job you asked for.** Amazon's search matches text, so the
  fetcher keeps only the hit whose id is the one in the URL.
- **Write a location the location rules can read**, such as `City, ST, USA`. Amazon's own
  `normalized_location` put a Massachusetts job in Wisconsin, so the fetcher builds it from
  the city and region instead. Surprises like this are why Step 6 uses a real response.

### Step 5: register it

In [registry.py](../jobfilter/fetchers/registry.py):

```python
Platform.AMAZON: AmazonFetcher(self._client),
```

### Step 6: test it offline, against a real response

Save one real response and one "not found" response under `tests/fixtures/<site>/`. Look
through them first: a fixture must not contain cookies, tokens or anyone's personal data.

Then write `tests/test_<site>.py` with a fake transport that serves those files.
[test_amazon.py](../tests/test_amazon.py) is a template: one test per thing that could go
wrong (the fields, the location, a missing job, a hit for a different job, an answer that
is not JSON) and one `@pytest.mark.live` test that notices if the real site changes shape.

### Step 7: try it for real

```bash
.venv/bin/python -E -m jobfilter check 'https://www.amazon.jobs/en/jobs/10568411/...' --no-db
```

`--no-db` prints the result without storing it. Once it looks right, paste the URL on the
**Add jobs** page.

### Step 8 (optional): the whole company

A fetcher that can also list every job at a company gets "check every other job at this
company" and board refresh for free. Add `fetch_board(slug)`; add `fetch_board_url(parsed)`
if a URL can name a board, and have `url_parser` return `job_id=None` for it; add
`list_board(slug)` too if listing is cheap but each job costs a request.
[smartrecruiters.py](../jobfilter/fetchers/smartrecruiters.py) and
[rippling.py](../jobfilter/fetchers/rippling.py) are short examples of all three, and a
good starting point when a site has a documented API of its own.

Decide first what "the company" is. Amazon as a whole lists tens of thousands of jobs and
its search stops at 10,000, so the board there is a **team**: every job names one
(`team-amazon-robotics`), the search filters on it, and a job's company slug is its team
(`amazon-robotics`). That one choice is what makes `amazon.jobs/en/teams/amazon-robotics`
a board URL, "every other job" mean the team, and refresh re-read just that team.

Two things a board must get right:

- **An unreadable board must raise, not return an empty list.** A refresh treats "the
  board lists nothing" as "every stored job was taken down" and marks them closed.
- **One job, one row.** If the same job can turn up under two slugs (an Amazon job stored
  before its team was known), add the platform to `_SITE_WIDE_JOB_IDS` in `store.py` so it
  is moved instead of stored twice.

---

## 7. Add a filter or a page

A filter passes through four places, always the same four:

1. **`PostingFilter`** in [store.py](../jobfilter/storage/store.py): add a field and its
   condition in `where()`. Add a `filtered_<name>_counts` method beside the others if the
   page should show counts.
2. **`_filters`** in [api.py](../jobfilter/web/api.py): read it from the query string.
   Return the counts from `postings()`.
3. **`filterParams`** in [app.js](../jobfilter/web/static/app.js): add it to the query.
   Add a `Set` to `state`, a `renderChecks(...)` call in `renderFacets`, and a
   `wireChecks(...)` line.
4. **`index.html`**: a `<div class="check-list" id="...">` in the filter pane.

The model-verdict filter was added exactly this way; search the code for `verdict` to see
all four.

A page is a `<section class="view" data-view="name">` in `index.html`, a link in the
sidebar, and its name in `PAGES` in [pages.js](../jobfilter/web/static/pages.js).

The page has no build step: edit the files and reload. Its tests drive a real headless
Chrome (`tests/test_web_ui*.py`) and are skipped if Chrome is not installed.

---

## 8. Store something new

1. Add the table or column to [schema.sql](../jobfilter/storage/schema.sql), which always
   describes the newest shape. Add it to the `posting_overview` view if the page needs it.
2. In `store.py`, raise `SCHEMA_VERSION` and add an entry to `MIGRATIONS` that brings an
   existing database forward. Dropping and recreating the view is the usual entry; a new
   table needs nothing more, because `schema.sql` creates it.
3. Add a test in `tests/test_storage.py` or `tests/test_applications.py` that opens a
   database of the previous version and checks the data survived.

Decide which kind of data it is:

- **Yours** (`applications`: your marks and notes). Nothing may rewrite it but you.
- **Fetched** (`postings`, `classifications`). Kept as history; a refresh never deletes.
- **Derived** (`role_screens`). Rebuilt whole by `screen`, so it can be dropped in a
  migration.

Back up before trying a migration on your own data: `cp jobfilter.db jobfilter.db.bak-$(date +%F)`.

---

## Before you call it done

- `make test` passes.
- The new behavior has a test that fails without your change. For rules, that is a line
  in `tests/cases.json`; for a fetcher, a saved real response.
- The docs say how to run and test it: a site's links and quirks in
  [SITES.md](SITES.md), a design decision in [HOW-IT-WORKS.md](HOW-IT-WORKS.md), a new
  endpoint in [HTTP-API.md](HTTP-API.md), a rules or database version in
  [CHANGELOG.md](CHANGELOG.md).
- No API key, cookie or personal data is in a committed file. Keys belong in the
  environment or in `~/.config/jobfilter/`.
