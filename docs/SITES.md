# Supported sites

Every job site jobfilter reads, the links it accepts, and how each one is fetched. All of
them are read through public, unauthenticated JSON: no logins, no scraping of rendered
pages.

- [Links: what is accepted](#links-what-is-accepted)
- [Links that do not work, and what to do](#links-that-do-not-work-and-what-to-do)
- [Fetching: the shared rules](#fetching-the-shared-rules)
- [Whole companies](#whole-companies)
- Per site: [Ashby](#ashby) · [Greenhouse](#greenhouse) · [Lever](#lever) ·
  [Workday](#workday) · [Dover](#dover) · [Rippling](#rippling) ·
  [SmartRecruiters](#smartrecruiters) · [Workable](#workable) · [Eightfold](#eightfold) ·
  [Amazon Jobs](#amazon-jobs)
- [Posting dates](#posting-dates)

---

## Links: what is accepted

[url_parser.py](../jobfilter/url_parser.py) turns a link into
`ParsedUrl(platform, slug, job_id, url)`. It does no network I/O and never raises. On every
shape the scheme and `www.` are optional, the query string and fragment are ignored
unless named below, and a trailing `/`, `/application` or `/apply` is tolerated.

| Platform | Link |
| --- | --- |
| `ashby` | `jobs.ashbyhq.com/{slug}/{job_id}` |
| `ashby` | any company careers page with `?ashby_jid={job_id}`, e.g. `corvus-robotics.com/careers?ashby_jid=52fbad75-…`. The slug is looked up |
| `greenhouse` | `boards.greenhouse.io/{slug}/jobs/{job_id}` and `job-boards.greenhouse.io/{slug}/jobs/{job_id}` |
| `greenhouse` | `job-boards.greenhouse.io/embed/job_app?for={slug}&token={job_id}` (the iframe a careers page embeds) |
| `greenhouse` | `{boards,job-boards}.greenhouse.io/{slug}?gh_jid={job_id}` |
| `greenhouse` | any company careers page with `?gh_jid={job_id}`, e.g. `nuro.ai/careersitem?gh_jid=7351066`. The slug is looked up |
| `lever` | `jobs.lever.co/{slug}/{job_id}` |
| `workday` | `{tenant}.wd{N}.myworkdayjobs.com/…/job/…_{id}`, the search pane's `/details/…_{id}`, and board links |
| `dover` | `app.dover.com/apply/{Company Name}/{job_id}`. The slug is the name slugified (`alloy-robotics`) |
| `rippling` | `ats.rippling.com/[{locale}/]{slug}/jobs/{job_id}`, and the board `ats.rippling.com/{slug}/jobs` |
| `smartrecruiters` | `jobs.smartrecruiters.com/{Company}/{id}-{title}`, the board on `jobs.` or `careers.smartrecruiters.com`, and the API addresses |
| `workable` | `apply.workable.com/{account}/j/{shortcode}`, `apply.workable.com/j/{shortcode}`, `jobs.workable.com/view/{view_id}/…`, board links and the API addresses |
| `eightfold` | `/careers/job/{id}`, `/careers?pid={id}` and `/api/apply/v2/jobs/{id}` on any host; search links (see [Eightfold](#eightfold)) |
| `amazon` | `amazon.jobs/[{locale}/]jobs/{id}[/{title}]`, `account.amazon.jobs/jobs/{id}/apply`, and team links |

Anything else returns `platform="unsupported"` with `slug=None, job_id=None`. That
includes a known host whose path is not a job (`jobs.ashbyhq.com/acme`,
`boards.greenhouse.io/acme/job/123`). The original string is always kept in `.url`, so the
link is reported rather than dropped.

### Slug lookups for company careers pages

A careers page that embeds Greenhouse or Ashby often puts only the job id in its link.

- **Greenhouse** (`gh_jid`): the fetcher requests
  `boards.greenhouse.io/embed/job_app?token={job_id}` without following redirects.
  Greenhouse answers `301` to a URL containing `for={slug}`, or `404` for an unknown job.
  That is one extra request per link. It relies on a legacy redirect, so `pytest -m live`
  checks that it still works.
- **Ashby** (`ashby_jid`): Ashby has no lookup by job id alone. The fetcher reads the page
  the link points to and takes the slug its embed names
  (`<script src="jobs.ashbyhq.com/corvus-robotics/embed">`), then falls back to the
  domain's labels (`corvus-robotics` from `www.corvus-robotics.com`, skipping `www`,
  `careers`, `jobs`). A slug is only accepted once its board lists the job, so a wrong
  guess costs one 404, never a wrong posting.

---

## Links that do not work, and what to do

| You have | Why it fails | Use instead |
| --- | --- | --- |
| A company careers page with the job number in the path but no `gh_jid` / `ashby_jid`, e.g. `https://x.company/careers/8807323002/` | The link does not say which job board holds the job, and an arbitrary company site is not fetched | Add the number as a query: `https://x.company/careers/8807323002/?gh_jid=8807323002` (Greenhouse) or `?ashby_jid=…` (Ashby). Or the board's own link: `https://job-boards.greenhouse.io/x9/jobs/8807323002` |
| A Greenhouse, Lever or Ashby **API** address, e.g. `boards-api.greenhouse.io/v1/boards/x9/jobs/8807323002` | Only job page links are parsed for these three; the API host is not in the parser's table | The job page: `job-boards.greenhouse.io/x9/jobs/8807323002` |
| An Ashby, Greenhouse or Lever **company board** (`jobs.lever.co/acme`) | Board links are not parsed for these three yet | One job link from that company, plus `--expand` |
| An Eightfold search page on a host jobfilter does not know | Search pages are only recognized on known hosts | The search's API form (`/api/apply/v2/jobs?domain=…`), or add the host to `_EIGHTFOLD_HOSTS` in `url_parser.py` |
| LinkedIn, Indeed, or a careers site with no public JSON | No fetcher | The browser extension: badge on LinkedIn / Indeed / Workday, **Scan this page** elsewhere |

**Telling which board a careers page uses.** Look at where its **Apply** button or
application form points, or search the page source for `greenhouse.io`, `ashbyhq.com`,
`lever.co`, `myworkdayjobs.com`. A Greenhouse job's id is the number in the Greenhouse
link; it is often also the number in the company's own link. For example, Greenhouse
reports the `x9` job above's own address as
`https://x.company/careers/8807323002?gh_jid=8807323002`.

---

## Fetching: the shared rules

`FetcherRegistry.fetch(parsed) -> Posting`. One fetcher per platform behind the same
interface ([base.py](../jobfilter/fetchers/base.py)), all sharing one `HttpClient`
([http.py](../jobfilter/fetchers/http.py)): a 10 s timeout (`--timeout`), and one retry with
backoff on transport errors, timeouts and 429 / 500 / 502 / 503 / 504. A 4xx is not
retried.

Every `Posting` has the same fields: `platform`, `company_slug`, `job_id`, `title`,
`location`, `url`, `description_text` (plain text, HTML stripped, section headings kept)
and `raw` (the original JSON).

Errors are typed, and each becomes a visible row, never a gap:

| Exception | Meaning |
| --- | --- |
| `BoardNotFound` | the company does not exist on that site |
| `JobNotFound` | the company exists but has no such job: closed or unlisted |
| `TransportFailure` | network error, timeout or 5xx that survived the retry |
| `RateLimited` | the site kept refusing requests (see [Eightfold](#eightfold)) |
| `UnsupportedPlatform` | no fetcher registered for that platform |

### Where the endpoints come from

| Site | Source |
| --- | --- |
| Greenhouse | [Job Board API](https://developers.greenhouse.io/job-board.html) (documented) |
| Lever | [Postings API](https://github.com/lever/postings-api) (documented) |
| Ashby | [Job Posting API](https://developers.ashbyhq.com) (documented) |
| SmartRecruiters | [Posting API](https://developers.smartrecruiters.com) (documented) |
| Workday, Amazon Jobs, Eightfold, Workable, Rippling, Dover | undocumented: observed in the browser's network inspector on the sites' own pages, then reproduced with `curl` |

Undocumented endpoints can change without notice. Each has `@pytest.mark.live` tests,
and `make test-live` is how you find out one has moved.
[EXTENDING.md § 6](EXTENDING.md#step-1-find-where-the-site-keeps-its-data) shows the
inspection method.

---

## Whole companies

Two ways to read every job at a company:

- **`--expand`** (or the web page's *Also check every other job at these companies*):
  after the links you gave, read the rest of each company seen.
- **A board link**, where the site has one: Workday, Rippling, SmartRecruiters, Workable,
  Eightfold searches, Amazon teams.

Results carry an `origin`: `requested` (you gave the link) or `discovered`. A job given by
its own link is not repeated.

| Site | Cost of a whole company |
| --- | --- |
| Ashby | none: fetching one job already downloaded the board |
| Greenhouse, Lever, Workable | one request (the board comes with full text) |
| Workday | one request per 20 jobs, plus one per job |
| Eightfold | one request per 10 jobs, plus one per job, paced (see below) |
| Rippling, Dover, SmartRecruiters | the list, plus one request per job |
| Amazon | one request per 100 jobs of the team |

Board reading is an optional capability: a fetcher that can list a company implements
`BoardFetcher` (`fetch_board`) and optionally `fetch_board_url`, `list_board` and
`learn`. `registry.supports_boards(platform)` answers at runtime, so a fetcher that can
only read single jobs is never asked to pretend.

---

## Ashby

| Endpoint | Used for |
| --- | --- |
| `GET api.ashbyhq.com/posting-api/job-board/{slug}` | the whole board, with `descriptionPlain` |

Ashby has no per-job endpoint, so the fetcher caches the board per slug for the run:
several links from one company cost one request. The cache lives on the registry, not in
a module global, so each run starts cold.

## Greenhouse

| Endpoint | Used for |
| --- | --- |
| `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}` | one job; `content` is HTML-escaped HTML, unescaped once then stripped |
| `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | the board with full text (without `content=true` it is titles only) |

Greenhouse cannot tell a missing board from a missing job: both are `404`, reported as
`JobNotFound`.

## Lever

| Endpoint | Used for |
| --- | --- |
| `GET api.lever.co/v0/postings/{slug}/{job_id}?mode=json` | one job |
| `GET api.lever.co/v0/postings/{slug}?mode=json` | the board |

The description is `descriptionPlain`, each `lists` section, **and `additionalPlain`**:
that trailing block is where companies tend to put export-control and citizenship notices
next to their EEO text, so leaving it out would produce false `OPEN` verdicts. Set
`INCLUDE_ADDITIONAL = False` in [lever.py](../jobfilter/fetchers/lever.py) to drop it.

## Workday

```bash
jobfilter check "https://bostondynamics.wd1.myworkdayjobs.com/Boston_Dynamics/job/Waltham-MA/Staff-Full-Stack-Software-Engineer_R2879"
jobfilter check "https://bostondynamics.wd1.myworkdayjobs.com/en-US/Boston_Dynamics"     # every job
jobfilter check "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite?q=robotics&locationHierarchy1=2fcb99c455831013ea52fb338f2932d8"   # a filtered board
```

Every Workday career site is a single-page app over an unauthenticated JSON API:

| Endpoint | Used for |
| --- | --- |
| `POST /wday/cxs/{tenant}/{site}/jobs` | search: 20 jobs per page, titles and paths, no descriptions |
| `GET /wday/cxs/{tenant}/{site}/job/{path}` | one posting, description as HTML |

Jobs are fetched 6 at a time with progress on stderr. A board of ~75 jobs takes seconds;
one of ~2,600 takes about 4 minutes. A search on a board link (`?q=`) and facets are
kept.

What the API does, and how the fetcher handles it:

| Quirk | Handling |
| --- | --- |
| `total` is only on the first page; later pages say 0 | page count comes from page 1 |
| a search stops at 2,000 results; later offsets repeat jobs | split the search by a facet whose every value is under 2,000, merge, deduplicate |
| a facet the site doesn't have is HTTP 400 (`?source=LinkedIn`) | link parameters are forwarded only if the site lists them as facets |
| the job endpoint needs the title in the path, not just the req id | paths come from the link or the search results |
| a repost's path ends `_R3014-1` while `jobReqId` says `R3014` | the path id is the job id, as in the extension |
| no such site: 404; no such tenant: 422 | `BoardNotFound` |
| hundreds of job requests draw some HTTP 429 | failed jobs are retried one at a time after a pause |

**Choosing the facet to split by.** A facet every job has exactly one value of (job
family, time type) sums to the true total, and such facets agree; location facets sum
higher, since a job can be in several places. So the estimate is the largest sum two
facets agree on, and the split uses the facet covering that many jobs in the fewest
parts. If the merged list comes up short, a warning says so.

**Identity.** The company slug is the tenant (`nvidia`, `bostondynamics`), the same slug
the extension uses, so a job seen in the browser and fetched here is one stored posting.
A tenant can have several sites; `--expand` covers every site of it seen in the run.

## Dover

| Endpoint | Used for |
| --- | --- |
| `GET app.dover.com/api/v1/inbound/application-portal-job/{job_id}` | one job: title, company, locations, description HTML |
| `GET app.dover.com/api/v1/careers-page/{client_id}/jobs` | the company's jobs, no descriptions |

The company name in the link is display text, so the slug is that name slugified, the
same slug a LinkedIn posting from that company gets. Listing needs Dover's `client_id`,
which comes from a job response, so a company expands once one of its jobs was fetched in
the run. Dover's `visa_support` flag is kept in `raw` but not classified: verdicts quote
the posting's own sentences, and a flag is not one.

## Rippling

| Endpoint | Used for |
| --- | --- |
| `GET api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{job_id}` | one job |
| `GET api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` | every job, no descriptions, no paging |

The list has one entry per job *location*, so it is deduplicated by id, then each job is
read, 6 at a time. A description has two HTML parts, `company` and `role`; both are
classified. The application form's questions ("Are you legally authorized to work …?")
stay in `raw`: every applicant is asked them, so they say nothing about this posting.

## SmartRecruiters

```
GET api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}             one job
GET api.smartrecruiters.com/v1/companies/{company}/postings?offset=0&limit=100   the board, no text
```

A board costs one request per job on top of the list (~700 jobs is about a minute). The
text is every section of the ad under its title. The company identifier is
case-insensitive; the slug is its lower-case form. An unknown company answers with an
empty list rather than a 404; that is reported as a missing board, so a refresh never
reads it as "every job closed".

## Workable

Workable shows a job in two places under two ids: the company's careers site
(`apply.workable.com/{account}/j/{shortcode}`) and Workable's own job board
(`jobs.workable.com/view/{view_id}/…`). One request returns every job of an account with
its full text:

```
GET apply.workable.com/api/v1/widget/accounts/{account}?details=true
```

so a single job is read from its company's board (as with Ashby), and a job is stored
under its account and shortcode whichever link was given.

- **A job-board link** names neither. The fetcher asks `jobs.workable.com/api/v1/jobs/{view_id}`
  for the title and company, takes the account from the company page's address
  (`jobs-at-octavian-robotics`), and finds the job on that account's board by its title
  (and city, when two openings share one). If that fails, the posting is still returned,
  built from the job-board answer and filed under the view id.
- **A short link** (`apply.workable.com/j/{shortcode}`) is followed to its account.
- **Board links:** `apply.workable.com/{account}`, the job board's company page
  (`jobs.workable.com/company/{id}/jobs-at-{account}`), and the two API addresses.

## Eightfold

```bash
jobfilter check 'https://careers.qualcomm.com/careers/job/446720880729'
jobfilter check 'https://careers.qualcomm.com/careers?query=robotics&location=United%20States'   # a search
```

Eightfold AI hosts career sites on the employer's own domain: Qualcomm
(`careers.qualcomm.com`), Microsoft (`apply.careers.microsoft.com`), Netflix
(`explore.jobs.netflix.net`) and others, all on the same API:

```
GET /careers                                                  names the site's domain, sets a session cookie
GET /api/pcsx/position_details?position_id={id}&domain=...    one job, description as HTML
GET /api/pcsx/search?domain=...&query=&location=&start=N      a search: 10 jobs a page, no text
GET /api/apply/v2/jobs?domain=...&start=N&num=10              the same, on older sites
```

- **The company** is the host's name: `careers.qualcomm.com` is `qualcomm`. On Eightfold's
  own hosts (`app.eightfold.ai/careers?domain=qualcomm.com`) it is the `domain`.
- **Search links** are recognized on any host in their API form, and as
  `/careers?query=…` on the hosts in `_EIGHTFOLD_HOSTS` in `url_parser.py`. Add a
  company's host there to accept its search page.
- **Two API generations.** Some sites refuse the older search, others the newer; the
  fetcher tries the newer first and remembers which answered.
- **Pace.** Some of these sites sit behind a firewall that blocks a burst of requests
  for minutes. So requests to one host take turns, 2 a second, across every thread. A 405
  or 429 holds every request to that host for 60 s, then goes on at half the pace. A host
  still refusing after ~15 minutes is given up for the run: its remaining jobs fail as
  `RateLimited` without being sent, and a later `jobfilter refresh --company <slug>`
  fetches only what is missing. A whole ~2,000-job company takes about 20 minutes.

## Amazon Jobs

```bash
jobfilter check 'https://www.amazon.jobs/en/jobs/10568411/software-development-engineer-amazon-robotics'
jobfilter check 'https://www.amazon.jobs/en/teams/amazon-robotics'      # a whole team
```

A job page has no JSON of its own, so the fetcher asks the site's search for the job's
number (`/en/search.json?base_query={id}`) and keeps the hit whose `id_icims` matches.
This endpoint is undocumented; `pytest -m live tests/test_amazon.py` checks it still
answers in the expected shape.

- **A team is the board.** Amazon as a whole lists tens of thousands of jobs and its
  search stops at 10,000, but every job names its team and the search filters on it. So a
  job's company slug is its team (`amazon-robotics`), a team page is a board link, and
  "every other job at this company" means the team. A job with no team is filed under
  `amazon`, which cannot be read as a board; nor can a "team" of more than 1,500 jobs.
- The text is the description plus "Basic Qualifications" and "Preferred Qualifications"
  under those headings.
- The location is built from each entry's city, region and country. Amazon's own
  `normalized_location` is not used: it has put a Massachusetts job in Wisconsin.
- Job ids are Amazon-wide, so a job stored before its team was known is moved to the
  team's company, not stored twice.

---

## Posting dates

[posted.py](../jobfilter/posted.py) reads one posting date per job from the board's own
field (`DATE_KEYS`):

| Site | Field | Site | Field |
|---|---|---|---|
| Workday | `startDate` | Amazon | `posted_date` |
| Greenhouse | `first_published` | Rippling | `createdOn` |
| Eightfold | `postedTs` | Workable | `published_on` |
| SmartRecruiters | `releasedDate` | Dover | `created` |
| Lever | `createdAt` (ms) | Ashby | `publishedAt` |

LinkedIn (and any page the extension reads) gives only "3 days ago"; that is counted back
from when the page was read and marked approximate. Indeed and pasted pages have no date.
Some boards keep the first date across a repost (Greenhouse); Workday's can move when a
job is re-posted.

---

## Testing a fetcher

Every fetcher is tested offline against saved real responses in `tests/fixtures/<site>/`,
served through `httpx.MockTransport`, so the production path (httpx, retries, JSON
decoding) runs unchanged without a network. Each also has `@pytest.mark.live` tests that
re-fetch real postings, which is how you learn an API drifted and a fixture needs
re-saving:

```bash
.venv/bin/python -E -m pytest tests/test_fetchers.py tests/test_url_parser.py -q
.venv/bin/python -E -m pytest -m live tests/test_workday.py
```

To add a site, follow [EXTENDING.md § 6](EXTENDING.md#6-add-a-job-site-amazon-jobs-step-by-step).
