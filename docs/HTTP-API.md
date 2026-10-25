# Local server API

`jobfilter serve` listens on `127.0.0.1:8000` (`--host`, `--port`). The web page and the
browser extension are its only intended clients. It has no authentication and sends no
CORS headers, so keep it bound to localhost.

All bodies are JSON unless stated. Routes live in [api.py](../jobfilter/web/api.py).

## Postings and companies

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/stats` | totals, per-company summary, label and region lists, every listed place with its count, `outcomes`, `closing_soon_days`. Each company row has `refreshable`, `closed` and `refresh` (`{at, status, listed, error, changes, changed}`, or `null` if never refreshed) |
| GET | `/api/postings` | one page of rows plus `total`, `limit`, `offset` and per-filter counts (`label_counts`, `region_counts`, `app_counts`, `stage_counts`, `outcome_counts`). Filters below |
| GET | `/api/postings/{id}` | detail, full text, classification history |
| GET | `/api/companies` | per-company counts under the same filters as `/api/postings`; each row carries `all_total`, its unfiltered count |
| GET | `/api/place?q=` | how a typed place is read ("every US location", "anywhere in California") and how many postings it matches |
| GET | `/api/failures` | links that never classified |
| GET | `/api/calendar?kind=applied\|added&from=YYYY-MM-DD&to=YYYY-MM-DD` | `{"days": {"2026-10-04": 7, …}}` (days with none left out), plus any `/api/postings` filter. 400 on a bad kind or range |

### `/api/postings` filters

Every filter but `q` repeats (`?exclude=a&exclude=b`).

| Parameter | Keeps |
| --- | --- |
| `label` | these restriction labels |
| `company` / `exclude` | only / not these company slugs |
| `region` | `us`, `outside_us`, `unknown` |
| `place` / `exclude_place` | postings listing / not listing a place |
| `q` | text in title, company name or evidence |
| `app` | your mark: `applied`, `maybe`, `skipped`, `none` |
| `stage` | where the role funnel stopped it (`model` = passed the rules) |
| `verdict` | the model's verdict: `best`, `fit`, `stretch`, `skip`, `unknown` (needs a look) |
| `outcome` | `oa`, `interview`, `offer`, `rejected`, `waiting` |
| `resume` | a resume id, or `none` |
| `changed=1` | postings their company's latest refresh found different |
| `closing=1` | closing soon (see `closing_soon_days`) |
| `posted=N` | went up in the last N days |
| `applied_on=DAY` / `added_on=DAY` | one day's postings |
| `order` | `newest` (default), `posted`, `posted_asc`, `deadline` |
| `limit`, `offset` | paging; `limit` defaults to 100, max 1000 |

## Checking and processing

| Method | Path | Does |
| --- | --- | --- |
| POST | `/api/check` | `{"urls": "…", "expand"?: bool}` (links one per line, at most 200) → run parse → fetch → classify and record it |
| POST | `/api/refresh` | start a board refresh in the background. Body (optional): `{"companies": [{"platform", "slug"}], "deep": false}`. `202` with the state, `409` if one is running, `400` for a company not stored or with no board |
| GET | `/api/refresh` | `{running, deep, total, done, current, started_at, finished_at, results, error}` |
| POST | `/api/screen` | run the role funnel now; returns postings per stage |
| POST | `/api/process` | screen, then have the model read what passed and is unread. `202`, or `409` while one runs |
| GET | `/api/process` | progress, plus what is waiting (`unscreened`, `unread`) |

```bash
curl -X POST localhost:8000/api/refresh -H 'Content-Type: application/json' \
     -d '{"companies": [{"platform": "ashby", "slug": "gritt"}]}'
curl localhost:8000/api/refresh
```

## Your marks, steps and resumes

| Method | Path | Does |
| --- | --- | --- |
| POST | `/api/postings/{id}/application` | `{"status": "applied" \| "skipped" \| "maybe" \| null, "note"?, "resume_id"?}`. A field left out is kept |
| POST | `/api/postings/{id}/outcome` | `{"stage": "oa" \| "interview" \| "offer" \| "rejected", "on"?: true}`. `"on": false` takes the step back. 409 unless the posting is marked Applied |
| POST | `/api/resumes?name=<file name>` | the file itself as the body, `Content-Type: application/octet-stream`. 201 with the resume; 200 and `"reused": true` for a file uploaded before; 400 for anything not a real PDF or DOCX, or over 5 MB |
| GET | `/api/resumes` | every resume, newest first, without its file. `used`: how many marks point at it. `results`: per resume (`none` for sent without one) `sent`, `waiting`, `oa`, `interview`, `offer`, `rejected`, `replied` |
| GET | `/api/resumes/{id}/file` | the file |
| POST | `/api/resumes/{id}` | `{"name": …}` renames it; `{"delete": true}` deletes it, 409 while it is attached |

```bash
curl -X POST --data-binary @resume.pdf -H 'Content-Type: application/octet-stream' \
     'http://127.0.0.1:8000/api/resumes?name=resume.pdf'
```

## For the browser extension

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/health` | `{ok, classifier, rules_version}` |
| POST | `/api/classify` | `{text, title?}` → `{decision}`. Stores nothing |
| POST | `/api/browsed` | `{site, job_id, url, title, company, location, posted_ago?, applicants?, text}`. Classifies server-side (a verdict in the body is ignored), stores it with origin `browsed`, returns `{posting_id, decision}` |

LinkedIn and Indeed postings are grouped under a slug of the company name, Workday's under
the tenant, other pages under the hostname, and keyed by URL without its query string, so
tracking parameters do not create duplicates.
