-- jobfilter schema, version 11.
--
-- Design notes:
--  * `classifications` is append-only history, not a column on `postings`.
--    Re-checking a URL adds a row only when the verdict actually changed, so
--    you can see when a posting was edited and the label flipped -- and Phase
--    2b can store an Ollama verdict next to the rule verdict on the same
--    posting for comparison.
--  * `content_hash` is sha256 of description_text. It is what tells a re-fetch
--    "the text changed" from "nothing happened".
--  * Failures are rows too (`run_items.status`), never dropped.
--  * A board refresh never deletes. A job its board stopped listing gets
--    `postings.closed_at`; what each refresh found is a row in
--    `posting_changes`, and that it ran at all a row in `board_refreshes`,
--    so "refreshed, nothing changed" differs from "never refreshed".
--  * `applications` is yours: what you did about a posting. Nothing rewrites
--    it but you. `role_screens` is derived: where the role funnel
--    (jobfilter.roles) stopped a posting, rewritten whole by each screen, so
--    it keeps no history. `role_facts` is what a model read in a posting:
--    kept across screenings, and stale once the posting's text changes.
--  * What came of an application is history too: `application_events` holds
--    one row per step reached (an OA, an interview, an offer, a rejection),
--    each with its date, so "rejected after an interview" is not flattened
--    into "rejected". `resumes` keeps the files themselves, so a copy of the
--    database carries them.
--  * All timestamps are ISO-8601 UTC strings: sortable as text, readable in a
--    sqlite3 shell, no timezone surprises.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    platform    TEXT NOT NULL,
    slug        TEXT NOT NULL,
    name        TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE (platform, slug)
);

CREATE TABLE IF NOT EXISTS postings (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    job_id           TEXT NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT,
    location         TEXT,
    description_text TEXT NOT NULL,
    raw_json         TEXT,
    content_hash     TEXT NOT NULL,
    first_seen       TEXT NOT NULL,
    last_fetched     TEXT NOT NULL,
    -- Job-board listing signals, verbatim ("Reposted 2 weeks ago", "Over 100
    -- applicants"). Relative, so listing_seen_at records when they were read.
    -- A later read that misses them keeps the old values.
    posted_ago       TEXT,
    applicants       TEXT,
    listing_seen_at  TEXT,
    -- When a board refresh stopped finding the job. NULL while it is listed;
    -- any later successful fetch clears it.
    closed_at        TEXT,
    -- The apply-by date the description states, else the day before
    -- Workday's endDate (jobfilter.deadline), as YYYY-MM-DD, with the sentence
    -- it came from. apply_by_firm is 0 for "accepted at least until": a
    -- minimum, not a close. Re-read on each fetch.
    apply_by         TEXT,
    apply_by_firm    INTEGER,
    apply_by_text    TEXT,
    -- The day the posting went up (jobfilter.posted), as YYYY-MM-DD: the
    -- board's own date, else counted back from posted_ago, in which case
    -- posted_on_approx is 1 for "2 weeks ago" and the like.
    posted_on        TEXT,
    posted_on_approx INTEGER,
    UNIQUE (company_id, job_id)
);

CREATE TABLE IF NOT EXISTS classifications (
    id                 INTEGER PRIMARY KEY,
    posting_id         INTEGER NOT NULL REFERENCES postings (id) ON DELETE CASCADE,
    classifier_name    TEXT NOT NULL,
    rules_version      INTEGER,
    label              TEXT NOT NULL,
    confidence         REAL,
    evidence           TEXT,
    matched_rule       TEXT,
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    n_urls      INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS run_items (
    id                INTEGER PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    posting_id        INTEGER REFERENCES postings (id) ON DELETE SET NULL,
    classification_id INTEGER REFERENCES classifications (id) ON DELETE SET NULL,
    url_as_given      TEXT NOT NULL,
    status            TEXT NOT NULL,
    label             TEXT NOT NULL,
    origin            TEXT NOT NULL DEFAULT 'requested',
    error_type        TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL
);

-- One row per thing a refresh found different about a posting.
-- kind: new | edited | relabeled | closed | reopened (models.Change).
CREATE TABLE IF NOT EXISTS posting_changes (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    posting_id  INTEGER NOT NULL REFERENCES postings (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

-- One row per company per refresh run. status: ok | error.
CREATE TABLE IF NOT EXISTS board_refreshes (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    company_id  INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    listed      INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL
);

-- A resume you uploaded. sha256 is of `data`: the same file uploaded twice is
-- one row, so every application it went to counts together.
CREATE TABLE IF NOT EXISTS resumes (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    sha256        TEXT NOT NULL UNIQUE,
    size          INTEGER NOT NULL,
    data          BLOB NOT NULL,
    uploaded_at   TEXT NOT NULL
);

-- One row per posting you marked. status: applied | skipped | maybe
-- (models.AppStatus). Clearing a mark deletes the row. resume_id is the
-- resume you sent, if you said.
CREATE TABLE IF NOT EXISTS applications (
    posting_id  INTEGER PRIMARY KEY REFERENCES postings (id) ON DELETE CASCADE,
    status      TEXT NOT NULL CHECK (status IN ('applied', 'skipped', 'maybe')),
    note        TEXT,
    updated_at  TEXT NOT NULL,
    resume_id   INTEGER REFERENCES resumes (id) ON DELETE SET NULL
);

-- What came of an application: one row per step reached, at most one of each.
-- stage: oa | interview | offer | rejected (models.Outcome). Clearing the
-- posting's mark deletes its rows.
CREATE TABLE IF NOT EXISTS application_events (
    posting_id  INTEGER NOT NULL REFERENCES postings (id) ON DELETE CASCADE,
    stage       TEXT NOT NULL CHECK (stage IN ('oa', 'interview', 'offer', 'rejected')),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (posting_id, stage)
);

-- Where the role funnel stopped each posting. stage: location | restricted |
-- non_engineering | seniority | off_focus | mech_elec | model (roles.Stage).
-- A posting with no row has not been screened yet.
CREATE TABLE IF NOT EXISTS role_screens (
    posting_id     INTEGER PRIMARY KEY REFERENCES postings (id) ON DELETE CASCADE,
    stage          TEXT NOT NULL,
    reason         TEXT NOT NULL,
    evidence       TEXT,
    category       TEXT,
    rule_id        TEXT,
    focus_by       TEXT,
    focus_evidence TEXT,
    -- best | fit | stretch | skip | unknown (roles.Verdict), for a posting that
    -- passed the rules and has been read by a model. NULL: not read yet.
    verdict        TEXT,
    verdict_reason TEXT,
    -- How much of the posting is about your own skills (roles.match): the sum
    -- of the weights found, and the names found as a JSON list.
    match_score    INTEGER NOT NULL DEFAULT 0,
    match_skills   TEXT NOT NULL DEFAULT '[]',
    rules_version  INTEGER,
    updated_at     TEXT NOT NULL
);

-- What a model read in each posting (roles.reader). One row per posting, the
-- latest reading. content_hash is the text it read: an edited posting is read
-- again. facts_json is RoleFacts.to_dict().
CREATE TABLE IF NOT EXISTS role_facts (
    posting_id     INTEGER PRIMARY KEY REFERENCES postings (id) ON DELETE CASCADE,
    reader         TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,
    facts_json     TEXT NOT NULL,
    seconds        REAL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_postings_company    ON postings (company_id);
CREATE INDEX IF NOT EXISTS idx_changes_posting     ON posting_changes (posting_id, run_id);
CREATE INDEX IF NOT EXISTS idx_changes_run         ON posting_changes (run_id);
CREATE INDEX IF NOT EXISTS idx_board_refreshes     ON board_refreshes (company_id, run_id);
CREATE INDEX IF NOT EXISTS idx_class_posting       ON classifications (posting_id, classifier_name, id);
CREATE INDEX IF NOT EXISTS idx_class_label         ON classifications (label);
CREATE INDEX IF NOT EXISTS idx_run_items_run       ON run_items (run_id);
CREATE INDEX IF NOT EXISTS idx_run_items_posting   ON run_items (posting_id);

-- The newest classification per (posting, classifier). With only the rule
-- classifier this is one row per posting; Phase 2b naturally yields one row
-- per classifier, which is what makes them comparable.
CREATE VIEW IF NOT EXISTS latest_classifications AS
SELECT c.*
FROM classifications c
WHERE c.id = (
    SELECT MAX(x.id)
    FROM classifications x
    WHERE x.posting_id = c.posting_id
      AND x.classifier_name = c.classifier_name
);

-- Everything the UI needs for one row of its table.
CREATE VIEW IF NOT EXISTS posting_overview AS
SELECT
    p.id            AS posting_id,
    co.id           AS company_id,
    co.platform     AS platform,
    co.slug         AS company_slug,
    co.name         AS company_name,
    p.job_id        AS job_id,
    p.url           AS url,
    p.title         AS title,
    p.location      AS location,
    p.first_seen    AS first_seen,
    p.last_fetched  AS last_fetched,
    p.posted_ago      AS posted_ago,
    p.applicants      AS applicants,
    p.listing_seen_at AS listing_seen_at,
    p.closed_at       AS closed_at,
    p.apply_by        AS apply_by,
    p.apply_by_firm   AS apply_by_firm,
    p.apply_by_text   AS apply_by_text,
    p.posted_on       AS posted_on,
    p.posted_on_approx AS posted_on_approx,
    p.content_hash  AS content_hash,
    -- How we first came across this posting: you asked for it, or expanding
    -- its company's board turned it up.
    (SELECT ri.origin FROM run_items ri
      WHERE ri.posting_id = p.id ORDER BY ri.id LIMIT 1) AS origin,
    -- What the company's latest refresh found on this posting, e.g.
    -- "edited,relabeled"; NULL when it found nothing or never ran.
    (SELECT group_concat(kind) FROM (
        SELECT pc.kind FROM posting_changes pc
         WHERE pc.posting_id = p.id
           AND pc.run_id = (SELECT MAX(br.run_id) FROM board_refreshes br
                             WHERE br.company_id = co.id)
         ORDER BY pc.id)) AS changes,
    lc.id                 AS classification_id,
    lc.classifier_name    AS classifier_name,
    lc.label              AS label,
    lc.confidence         AS confidence,
    lc.evidence           AS evidence,
    lc.matched_rule       AS matched_rule,
    lc.matched_rules_json AS matched_rules_json,
    lc.created_at         AS classified_at,
    ap.status             AS app_status,
    ap.note               AS app_note,
    ap.updated_at         AS app_updated_at,
    ap.resume_id          AS resume_id,
    rz.name               AS resume_name,
    -- The steps an application reached, oldest first, each with its date:
    -- "oa:2026-10-07T18:02:11+00:00,rejected:2026-10-20T...". NULL while you
    -- are still waiting, and for a posting not marked applied.
    CASE WHEN ap.status = 'applied' THEN
        (SELECT group_concat(step) FROM (
            SELECT ae.stage || ':' || ae.created_at AS step
              FROM application_events ae
             WHERE ae.posting_id = p.id
             ORDER BY ae.created_at, ae.rowid))
    END AS outcome_steps,
    -- Where it stands: an offer, else a rejection, else the furthest step.
    CASE WHEN ap.status = 'applied' THEN
        (SELECT ae.stage FROM application_events ae
          WHERE ae.posting_id = p.id
          ORDER BY CASE ae.stage WHEN 'offer' THEN 0 WHEN 'rejected' THEN 1
                                 WHEN 'interview' THEN 2 ELSE 3 END
          LIMIT 1)
    END AS outcome,
    rs.stage              AS screen_stage,
    rs.reason             AS screen_reason,
    rs.evidence           AS screen_evidence,
    rs.focus_by           AS screen_focus_by,
    rs.focus_evidence     AS screen_focus_evidence,
    rs.verdict            AS verdict,
    rs.verdict_reason     AS verdict_reason,
    rs.match_score        AS match_score,
    rs.match_skills       AS match_skills,
    rf.reader             AS facts_reader,
    rf.facts_json         AS facts_json
FROM postings p
JOIN companies co ON co.id = p.company_id
LEFT JOIN latest_classifications lc ON lc.posting_id = p.id
LEFT JOIN applications ap ON ap.posting_id = p.id
LEFT JOIN resumes rz ON rz.id = ap.resume_id
LEFT JOIN role_screens rs ON rs.posting_id = p.id
LEFT JOIN role_facts rf ON rf.posting_id = p.id;
