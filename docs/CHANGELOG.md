# Changelog

What changed in the restriction rules, the database, and the features, in the order it was
built. The rules version is `"version"` in [rules.json](../rules.json); the database
version is `SCHEMA_VERSION` in [store.py](../jobfilter/storage/store.py). An existing
database upgrades itself on first open; back it up first.

## Restriction rules

Each change below was made by running every rule over the whole stored database and
reviewing each distinct match, and each fix has a case in `tests/cases.json` run by both
the Python and the JavaScript engine.

### Rules v4: "authorized to work in the United States" is not a signal

- The `authorized-to-work-us` rule is gone. Anyone with a work permit meets that
  requirement, so on its own it is `OPEN`.
- The same sentence with "without the need for current or future visa sponsorship" is
  caught by `without-sponsorship`: `INFO_NO_SPONSORSHIP`.
- A bare "maintain ongoing work authorization" stays `UNCLEAR`.

On the stored postings: 108 `UNCLEAR` became `OPEN`, 31 became `INFO_NO_SPONSORSHIP`.

### Rules v3: eligibility for a clearance is a restriction

- `able-to-obtain-clearance` is `RESTRICTED_CLEARANCE`, and also reads "eligible to
  receive", "eligible for a clearance" and "ability to travel …, and to obtain". It stays
  `UNCLEAR` when the clearance is conditional ("if required by the program", "may be
  required") or only preferred.
- `active-clearance` accepts `U.S.` and `or` between levels ("active U.S. Secret or TS
  clearance"); `named-clearance` accepts "Secret security clearance".

1,023 postings moved from `UNCLEAR` to `RESTRICTED_CLEARANCE`, and 116 from another
restricted label to clearance, which outranks them.

### Rules v2: fixes from auditing ~3,300 postings

| Problem in real postings | Fix |
| --- | --- |
| "Legal and **Public Trust** teams" read as a clearance level | `Public Trust` counts only next to clearance wording |
| "creepage and **clearance**" (PCB), flight / separation / tool / customs clearance | `clearance-mention` skips physical and goods clearances |
| "clearance eligibility **may be required**", "clearance, **as required**" read as a bar | `clearance-required` needs "required" right after the clearance, unhedged |
| "travel as required, and obtain … a clearance" | `requires-clearance` needs "require(s)" governing the clearance |
| "TS/SCI clearance desired", "… is a plus" read as required | preferred-only sentences are `UNCLEAR` |
| "Familiarity with ITAR, EAR, DFARS … requirements" read as a US-person bar | regime lists with duty words are `UNCLEAR` |
| Bare "EAR" was a bar while bare "ITAR" was not | EAR behaves like ITAR |
| "**US Citizen** is required" was missed | `us-citizenship-required` accepts "citizen" as well as "citizenship" |
| "- U.S. citizenship and ability to obtain a clearance" (no verb) was only `UNCLEAR` | new `us-citizenship-qualification` rule |

132 labels changed: 59 stayed restricted under a more precise label, 65 became `UNCLEAR`,
4 became `OPEN`, and 4 that the old rules missed became `RESTRICTED_CITIZEN`.

Also found while reading whole boards: hedged export-control wording ("this role **may
require** access to information subject to export control") had been called a bar; it is
`UNCLEAR` now, through `unless` on the four export-control and ITAR rules.

### Rules v1

The original rule set: word-boundary patterns, sentence-local negation, EEO exclusions,
open signals, severity and supersede. See
[HOW-IT-WORKS.md](HOW-IT-WORKS.md#the-restriction-classifier).

## Database schema

| Version | Added |
| --- | --- |
| 1 | `companies`, `postings`, `classifications`, `runs`, `run_items`; views `latest_classifications`, `posting_overview` |
| 2 | `run_items.origin` (`requested` / `discovered`), for whole-company expansion |
| 3 | `postings.posted_ago`, `applicants`, `listing_seen_at`: LinkedIn's listing line, with when it was read |
| 4 | `postings.closed_at`, `posting_changes`, `board_refreshes`: board refresh |
| 5 | `applications` (your marks and notes), `role_screens` (the role funnel's result) |
| 6 | `role_facts` (the model's readings); `role_screens.verdict`, `verdict_reason` |
| 7 | skill match score and reason on `role_screens` |
| 8 | `application_events` (OA / interview / offer / rejected), `resumes`, `applications.resume_id` |
| 9 | `postings.apply_by`, `apply_by_firm`, `apply_by_text`, read out of every stored description |
| 10 | apply-by dates read again, with Workday's `endDate` as the fallback |
| 11 | `postings.posted_on`, `posted_on_approx`, backfilled from stored JSON |

`role_screens` is derived, so its migrations drop and rebuild it: run `jobfilter screen`
after upgrading across 6 or 7.

## Features, in the order they were built

1. Link parsing for Ashby, Greenhouse and Lever.
2. Fetchers for the three, with offline tests against saved responses.
3. The rule classifier and `rules.json`.
4. The `check` command, JSONL output and summary tables.
5. SQLite storage with verdict history.
6. The local web page.
7. Whole-company expansion (`--expand`).
8. The JavaScript engine and the Python / JS parity corpus.
9. The Chrome extension (LinkedIn, Indeed, Workday, any page) and its server endpoints.
10. Fetchers for Workday, Dover, Rippling; board refresh.
11. Role screening: the rule funnel, marks, the Inbox and pages.
12. The local model reader, background screening and reading.
13. Fetchers for Amazon Jobs, SmartRecruiters, Workable, Eightfold.
14. Skill match and best fit.
15. Application steps, resumes and the Applications page.
16. Apply-by deadlines, posting dates, the Calendar.
