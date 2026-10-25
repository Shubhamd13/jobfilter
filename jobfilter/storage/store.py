"""SQLite persistence. Standard library only.

`Store` is the whole database API: the CLI and the web UI both go through it,
so there is one place where writes happen and one place to read the schema's
intent. See schema.sql for the design notes.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TypeVar

from ..deadline import posting_apply_by
from ..location import Region, in_region, place_matches, split_places
from ..posted import posted_on
from ..models import AppStatus, Change, Label, Outcome, Platform, Posting, Result, Status

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_VERSION = 11


def _add_resume_to_applications(connection: sqlite3.Connection) -> None:
    """Version 8: applications gains the resume you sent.

    Only where the table exists without it. A database from version 4 or
    older has no applications table yet, and schema.sql creates it whole.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(applications)")}
    if columns and "resume_id" not in columns:
        connection.execute(
            "ALTER TABLE applications"
            " ADD COLUMN resume_id INTEGER REFERENCES resumes (id) ON DELETE SET NULL"
        )
    connection.execute("DROP VIEW IF EXISTS posting_overview")


def _add_apply_by_to_postings(connection: sqlite3.Connection) -> None:
    """Version 9: postings gains the apply-by date its text states.

    Read out of every stored description now, so old postings have it too.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(postings)")}
    for column, kind in (("apply_by", "TEXT"), ("apply_by_firm", "INTEGER"), ("apply_by_text", "TEXT")):
        if column not in columns:
            connection.execute(f"ALTER TABLE postings ADD COLUMN {column} {kind}")
    _reread_apply_by(connection)
    connection.execute("DROP VIEW IF EXISTS posting_overview")


def _reread_apply_by(connection: sqlite3.Connection) -> None:
    """Version 10: apply-by dates read again, now with Workday's endDate too.

    Version 9 runs this as well, so a database coming from 8 reads twice;
    the second read finds what the first did.
    """
    rows = connection.execute(
        "SELECT id, description_text, raw_json, first_seen FROM postings"
    ).fetchall()
    connection.executemany(
        "UPDATE postings SET apply_by = ?, apply_by_firm = ?, apply_by_text = ? WHERE id = ?",
        [(*_apply_by_columns(row[1], _raw(row[2]), row[3]), row[0]) for row in rows],
    )


def _raw(raw_json: str | None) -> dict[str, Any]:
    """A stored raw_json as a dict; {} when missing or not an object."""
    try:
        raw = json.loads(raw_json or "{}")
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _apply_by_columns(
    description: str, raw: dict[str, Any], first_seen: str
) -> tuple[str | None, int | None, str | None]:
    """apply_by, apply_by_firm and apply_by_text for a posting."""
    try:
        seen = date.fromisoformat(first_seen[:10])
    except ValueError:
        seen = date.today()
    found = posting_apply_by(description, raw, seen)
    if found is None:
        return None, None, None
    return found.on.isoformat(), int(found.firm), found.sentence


def _add_posted_on_to_postings(connection: sqlite3.Connection) -> None:
    """Version 11: postings gains the day it went up, read from what is stored."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(postings)")}
    for column, kind in (("posted_on", "TEXT"), ("posted_on_approx", "INTEGER")):
        if column not in columns:
            connection.execute(f"ALTER TABLE postings ADD COLUMN {column} {kind}")
    rows = connection.execute(
        """
        SELECT p.id, co.platform, p.raw_json, p.posted_ago, p.listing_seen_at
          FROM postings p JOIN companies co ON co.id = p.company_id
        """
    ).fetchall()
    connection.executemany(
        "UPDATE postings SET posted_on = ?, posted_on_approx = ? WHERE id = ?",
        [(*_posted_on_columns(row[1], _raw(row[2]), row[3], row[4]), row[0]) for row in rows],
    )
    connection.execute("DROP VIEW IF EXISTS posting_overview")


def _posted_on_columns(
    platform: str, raw: dict[str, Any], posted_ago: str | None, listing_seen_at: str | None
) -> tuple[str | None, int | None]:
    """posted_on and posted_on_approx for a posting."""
    found = posted_on(platform, raw, posted_ago, listing_seen_at)
    if found is None:
        return None, None
    return found.on.isoformat(), int(found.approximate)


# Upgrades for databases that already exist. A fresh database runs schema.sql
# (which is always the current shape) and skips straight to SCHEMA_VERSION;
# an existing one applies every step above its own version, in order. A step
# is SQL, or a function where SQL alone cannot decide what to do.
MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection], None]] = {
    2: """
        ALTER TABLE run_items
          ADD COLUMN origin TEXT NOT NULL DEFAULT 'requested';

        DROP VIEW IF EXISTS posting_overview;
    """,
    3: """
        ALTER TABLE postings ADD COLUMN posted_ago TEXT;
        ALTER TABLE postings ADD COLUMN applicants TEXT;
        ALTER TABLE postings ADD COLUMN listing_seen_at TEXT;

        DROP VIEW IF EXISTS posting_overview;
    """,
    4: """
        ALTER TABLE postings ADD COLUMN closed_at TEXT;

        DROP VIEW IF EXISTS posting_overview;
    """,
    # applications and role_screens are new tables: schema.sql creates them.
    5: """
        DROP VIEW IF EXISTS posting_overview;
    """,
    # role_screens gains the verdict columns. It is derived data, so it is
    # dropped and rebuilt by the next screening rather than altered: an ALTER
    # would fail on a database coming from version 4, where it does not exist.
    6: """
        DROP TABLE IF EXISTS role_screens;
        DROP VIEW IF EXISTS posting_overview;
    """,
    # ...and again for the skill-match columns.
    7: """
        DROP TABLE IF EXISTS role_screens;
        DROP VIEW IF EXISTS posting_overview;
    """,
    # resumes and application_events are new tables: schema.sql creates them.
    8: _add_resume_to_applications,
    9: _add_apply_by_to_postings,
    10: _reread_apply_by,
    11: _add_posted_on_to_postings,
}
# Filter values for "has no row": a posting you have not marked, and one the
# role funnel has not placed yet.
NO_STATUS = "none"
UNSCREENED = "unscreened"
# ...and a posting no model has given a verdict on.
UNREAD = "unread"
# An application you have heard nothing about, and one with no resume attached.
WAITING = "waiting"
NO_RESUME = "none"
# set_application's default note and resume: leave whatever the posting has.
KEEP: Any = object()
# A posting whose apply-by date is this many days off, either side, is closing
# soon: before it, the deadline nears; after a minimum ("at least until"), the
# posting may come down any day.
CLOSING_SOON_DAYS = 7
DEFAULT_DB_FILENAME = "jobfilter.db"
DB_ENV_VAR = "JOBFILTER_DB"


class NotApplied(ValueError):
    """A step was recorded on a posting that is not marked applied."""


class ResumeInUse(ValueError):
    """A resume was to be deleted while an application still points at it."""


def default_db_path() -> Path:
    return Path(os.environ.get(DB_ENV_VAR) or DEFAULT_DB_FILENAME)


def utcnow() -> str:
    """ISO-8601 UTC. Sortable as text, readable in a sqlite3 shell."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(text: str) -> str:
    """Identity of a posting's text: lets a re-fetch tell 'changed' from 'same'."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_T = TypeVar("_T")


def _locked(method: Callable[..., _T]) -> Callable[..., _T]:
    """Serialize a database method behind the store's lock.

    The web UI runs on a ThreadingHTTPServer, so requests arrive on different
    threads, and a sqlite3 connection is bound to the thread that created it.
    One shared connection plus a reentrant lock keeps `:memory:` databases
    working -- a thread-local connection would give each thread its own empty
    database -- and is ample for a single-user local tool.
    """

    @functools.wraps(method)
    def wrapper(self: "Store", *args: Any, **kwargs: Any) -> _T:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    """Open a database, creating and migrating it if needed."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # Location regions are decided in Python (jobfilter.location), and
        # filtering on them in SQL keeps paging and counts in one query.
        self.connection.create_function(
            "jf_in_region", 2, in_region, deterministic=True
        )
        self.connection.create_function(
            "jf_place_matches", 2, place_matches, deterministic=True
        )
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    # -- lifecycle --------------------------------------------------------

    @_locked
    def migrate(self) -> int:
        """Bring this database up to SCHEMA_VERSION. Returns the version."""
        current = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if current == SCHEMA_VERSION:
            return SCHEMA_VERSION

        # user_version 0 means a brand new file: there is nothing to migrate,
        # and an ALTER TABLE would fail on tables that do not exist yet.
        if current > 0:
            for version in sorted(MIGRATIONS):
                if current < version:
                    step = MIGRATIONS[version]
                    if callable(step):
                        step(self.connection)
                    else:
                        self.connection.executescript(step)

        # schema.sql is the current shape and every statement in it is
        # IF NOT EXISTS, so it both creates a fresh database and restores
        # anything a migration dropped (the views).
        self.connection.executescript(SCHEMA_PATH.read_text())
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()
        return SCHEMA_VERSION

    @property
    def version(self) -> int:
        with self._lock:
            return self.connection.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writing ----------------------------------------------------------

    @_locked
    def record_run(self, source: str, results: Sequence[Result]) -> int:
        """Persist a whole run: every result, successes and failures alike."""
        run_id = self.start_run(source, len(results))
        for result in results:
            self.record_result(run_id, result)
        self.finish_run(run_id)
        return run_id

    @_locked
    def start_run(self, source: str, n_urls: int) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs (source, n_urls, started_at) VALUES (?, ?, ?)",
            (source, n_urls, utcnow()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    @_locked
    def finish_run(self, run_id: int) -> None:
        self.connection.execute(
            "UPDATE runs SET finished_at = ? WHERE id = ?", (utcnow(), run_id)
        )
        self.connection.commit()

    @_locked
    def record_result(self, run_id: int, result: Result) -> int | None:
        """Store one result. Returns its posting id, or None if it failed."""
        posting_id: int | None = None
        classification_id: int | None = None

        if result.status is Status.OK and result.posting is not None:
            posting_id = self.upsert_posting(result.posting, company_name(result.posting))
            classification_id = self.record_classification(posting_id, result)

        self.connection.execute(
            """
            INSERT INTO run_items (run_id, posting_id, classification_id,
                                   url_as_given, status, label, origin,
                                   error_type, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                posting_id,
                classification_id,
                result.url,
                str(result.status),
                str(result.label),
                str(result.origin),
                result.error_type,
                result.error,
                utcnow(),
            ),
        )
        self.connection.commit()
        return posting_id

    @_locked
    def upsert_posting(self, posting: Posting, company_name: str | None = None) -> int:
        posting_id = self._write_posting(posting, company_name)
        self._set_posted_on(posting_id, posting)
        return posting_id

    def _set_posted_on(self, posting_id: int, posting: Posting) -> None:
        """Read the posting date again, from the row as it now stands.

        After the write, because a read without listing signals keeps the old
        posted_ago, and the date counts back from whichever is kept.
        """
        row = self.connection.execute(
            "SELECT posted_ago, listing_seen_at FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
        self.connection.execute(
            "UPDATE postings SET posted_on = ?, posted_on_approx = ? WHERE id = ?",
            (
                *_posted_on_columns(
                    str(posting.platform), posting.raw, row["posted_ago"], row["listing_seen_at"]
                ),
                posting_id,
            ),
        )
        self.connection.commit()

    def _write_posting(self, posting: Posting, company_name: str | None) -> int:
        now = utcnow()
        digest = content_hash(posting.description_text)
        has_listing = posting.posted_ago is not None or posting.applicants is not None
        listing_seen_at = now if has_listing else None

        existing = self._posting_by_platform_job_id(posting)
        if existing is not None and company_name is None:
            # Seen before under a name; this read missed it. Keep the name.
            company_id = int(existing["company_id"])
        else:
            company_id = self._upsert_company(
                str(posting.platform), posting.company_slug, company_name, now
            )
        if existing is None:
            existing = self.connection.execute(
                "SELECT id FROM postings WHERE company_id = ? AND job_id = ?",
                (company_id, posting.job_id),
            ).fetchone()

        if existing is None:
            cursor = self.connection.execute(
                """
                INSERT INTO postings (company_id, job_id, url, title, location,
                                      description_text, raw_json, content_hash,
                                      first_seen, last_fetched,
                                      posted_ago, applicants, listing_seen_at,
                                      apply_by, apply_by_firm, apply_by_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    posting.job_id,
                    posting.url,
                    posting.title,
                    posting.location,
                    posting.description_text,
                    json.dumps(posting.raw, ensure_ascii=False),
                    digest,
                    now,
                    now,
                    posting.posted_ago,
                    posting.applicants,
                    listing_seen_at,
                    *_apply_by_columns(posting.description_text, posting.raw, now),
                ),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

        first_seen = self.connection.execute(
            "SELECT first_seen FROM postings WHERE id = ?", (existing["id"],)
        ).fetchone()[0]

        # Seen before: refresh the content, keep first_seen. Listing signals
        # are replaced as a set, and only by a read that found some: a
        # posted_ago from one read must not sit beside another read's time.
        # It was just fetched, so it is not closed, whatever a refresh said.
        self.connection.execute(
            """
            UPDATE postings
               SET company_id = ?, url = ?, title = ?, location = ?, description_text = ?,
                   raw_json = ?, content_hash = ?, last_fetched = ?, closed_at = NULL,
                   posted_ago = CASE WHEN ? THEN ? ELSE posted_ago END,
                   applicants = CASE WHEN ? THEN ? ELSE applicants END,
                   listing_seen_at = COALESCE(?, listing_seen_at),
                   apply_by = ?, apply_by_firm = ?, apply_by_text = ?
             WHERE id = ?
            """,
            (
                company_id,
                posting.url,
                posting.title,
                posting.location,
                posting.description_text,
                json.dumps(posting.raw, ensure_ascii=False),
                digest,
                now,
                has_listing,
                posting.posted_ago,
                has_listing,
                posting.applicants,
                listing_seen_at,
                *_apply_by_columns(posting.description_text, posting.raw, first_seen),
                existing["id"],
            ),
        )
        self.connection.commit()
        return int(existing["id"])

    @_locked
    def record_classification(
        self, posting_id: int, result: Result, rules_version: int | None = None
    ) -> int:
        """Append a classification, but only when the verdict actually changed.

        Re-checking an unchanged posting should not grow the history; a posting
        that was edited into a restriction should.
        """
        digest = (
            content_hash(result.posting.description_text) if result.posting else ""
        )
        matched = json.dumps(list(result.matched_rules))
        classifier = result.classifier_name or "unknown"

        latest = self.connection.execute(
            """
            SELECT * FROM classifications
             WHERE posting_id = ? AND classifier_name = ?
             ORDER BY id DESC LIMIT 1
            """,
            (posting_id, classifier),
        ).fetchone()

        unchanged = latest is not None and (
            latest["label"] == str(result.label)
            and latest["evidence"] == result.evidence
            and latest["matched_rule"] == result.matched_rule
            and latest["matched_rules_json"] == matched
            and latest["content_hash"] == digest
            and latest["rules_version"] == rules_version
        )
        if unchanged:
            return int(latest["id"])

        cursor = self.connection.execute(
            """
            INSERT INTO classifications (posting_id, classifier_name, rules_version,
                                         label, confidence, evidence, matched_rule,
                                         matched_rules_json, content_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                posting_id,
                classifier,
                rules_version,
                str(result.label),
                result.confidence,
                result.evidence,
                result.matched_rule,
                matched,
                digest,
                utcnow(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    @_locked
    def record_change(
        self, run_id: int, posting_id: int, kind: Change, detail: str | None = None
    ) -> None:
        """Note one thing a board refresh found different about a posting."""
        self.connection.execute(
            """
            INSERT INTO posting_changes (run_id, posting_id, kind, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, posting_id, str(kind), detail, utcnow()),
        )
        self.connection.commit()

    @_locked
    def close_postings(self, run_id: int, posting_ids: Sequence[int]) -> None:
        """Mark postings their board no longer lists. Kept, never deleted."""
        now = utcnow()
        for posting_id in posting_ids:
            self.connection.execute(
                "UPDATE postings SET closed_at = ? WHERE id = ?", (now, posting_id)
            )
            self.connection.execute(
                """
                INSERT INTO posting_changes (run_id, posting_id, kind, detail, created_at)
                VALUES (?, ?, ?, NULL, ?)
                """,
                (run_id, posting_id, str(Change.CLOSED), now),
            )
        self.connection.commit()

    @_locked
    def record_board_refresh(
        self,
        run_id: int,
        company_id: int,
        status: str,
        listed: int = 0,
        error: str | None = None,
    ) -> None:
        """Note that a company's board was re-read, whatever it found."""
        self.connection.execute(
            """
            INSERT INTO board_refreshes (run_id, company_id, status, listed, error,
                                         created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, company_id, status, listed, error, utcnow()),
        )
        self.connection.commit()

    @_locked
    def set_application(
        self,
        posting_id: int,
        status: str | None,
        note: str | None = KEEP,
        resume_id: int | None = KEEP,
    ) -> bool:
        """Mark a posting applied / skipped / maybe, or clear the mark with None.

        The note is left as it is unless one is given; "" or None removes it.
        So is the resume you sent: a resume id attaches one, None detaches it.
        updated_at is when the status was set: saving a note does not move it.
        Clearing the mark also forgets the steps the application reached.
        False when there is no such posting. ValueError on an unknown status
        or an unknown resume.
        """
        if status is not None:
            status = str(AppStatus(status))
        exists = self.connection.execute(
            "SELECT 1 FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
        if exists is None:
            return False
        if resume_id is not KEEP and resume_id is not None and self._resume(resume_id) is None:
            raise ValueError(f"no resume {resume_id}")

        if status is None:
            self.connection.execute(
                "DELETE FROM applications WHERE posting_id = ?", (posting_id,)
            )
            self.connection.execute(
                "DELETE FROM application_events WHERE posting_id = ?", (posting_id,)
            )
        else:
            self.connection.execute(
                """
                INSERT INTO applications (posting_id, status, note, updated_at, resume_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (posting_id) DO UPDATE
                   SET status = excluded.status,
                       note = CASE WHEN ? THEN applications.note ELSE excluded.note END,
                       resume_id = CASE WHEN ? THEN applications.resume_id
                                        ELSE excluded.resume_id END,
                       updated_at = CASE WHEN applications.status = excluded.status
                                         THEN applications.updated_at ELSE excluded.updated_at END
                """,
                (
                    posting_id,
                    status,
                    None if note is KEEP else (note or "").strip() or None,
                    utcnow(),
                    None if resume_id is KEEP else resume_id,
                    note is KEEP,
                    resume_id is KEEP,
                ),
            )
        self.connection.commit()
        return True

    @_locked
    def add_resume(
        self, name: str, filename: str, content_type: str, data: bytes
    ) -> tuple[dict[str, Any], bool]:
        """Keep a resume file. Returns it, and whether it was here already.

        A resume is its bytes: the same file uploaded again, under any name,
        is the resume already stored, so its applications count together.
        """
        digest = hashlib.sha256(data).hexdigest()
        known = self.connection.execute(
            "SELECT id FROM resumes WHERE sha256 = ?", (digest,)
        ).fetchone()
        if known is not None:
            return self._resume(known["id"]), True

        cursor = self.connection.execute(
            """
            INSERT INTO resumes (name, filename, content_type, sha256, size, data,
                                 uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, filename, content_type, digest, len(data), data, utcnow()),
        )
        self.connection.commit()
        return self._resume(cursor.lastrowid), False

    @_locked
    def rename_resume(self, resume_id: int, name: str) -> bool:
        """False when there is no such resume. ValueError on a blank name."""
        name = (name or "").strip()
        if not name:
            raise ValueError("a resume needs a name")
        cursor = self.connection.execute(
            "UPDATE resumes SET name = ? WHERE id = ?", (name, resume_id)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    @_locked
    def delete_resume(self, resume_id: int) -> bool:
        """False when there is no such resume. ResumeInUse while a mark points at it."""
        resume = self._resume(resume_id)
        if resume is None:
            return False
        if resume["used"]:
            raise ResumeInUse(f"resume {resume_id} is attached to {resume['used']} posting(s)")
        self.connection.execute("DELETE FROM resumes WHERE id = ?", (resume_id,))
        self.connection.commit()
        return True

    @_locked
    def set_outcome(self, posting_id: int, stage: str, on: bool = True) -> bool:
        """Record a step an application reached, or take it back with on=False.

        A step is kept once, with the date it was first recorded: recording it
        again changes nothing. False when there is no such posting. ValueError
        on an unknown stage; NotApplied when the posting is not marked applied.
        """
        stage = str(Outcome(stage))
        row = self.connection.execute(
            """
            SELECT ap.status FROM postings p
              LEFT JOIN applications ap ON ap.posting_id = p.id
             WHERE p.id = ?
            """,
            (posting_id,),
        ).fetchone()
        if row is None:
            return False
        if row["status"] != AppStatus.APPLIED:
            raise NotApplied(f"posting {posting_id} is not marked applied")

        if on:
            self.connection.execute(
                """
                INSERT INTO application_events (posting_id, stage, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT (posting_id, stage) DO NOTHING
                """,
                (posting_id, stage, utcnow()),
            )
        else:
            self.connection.execute(
                "DELETE FROM application_events WHERE posting_id = ? AND stage = ?",
                (posting_id, stage),
            )
        self.connection.commit()
        return True

    @_locked
    def replace_role_screens(
        self, screens: Iterable[RoleScreen], rules_version: int | None = None
    ) -> int:
        """Store a whole screening, replacing the last one. Returns how many rows."""
        now = utcnow()
        rows = [
            (s.posting_id, s.stage, s.reason, s.evidence, s.category, s.rule_id,
             s.focus_by, s.focus_evidence, s.verdict, s.verdict_reason,
             s.match_score, json.dumps(list(s.match_skills), ensure_ascii=False),
             rules_version, now)
            for s in screens
        ]
        self.connection.execute("DELETE FROM role_screens")
        self.connection.executemany(
            """
            INSERT INTO role_screens (posting_id, stage, reason, evidence, category,
                                      rule_id, focus_by, focus_evidence,
                                      verdict, verdict_reason,
                                      match_score, match_skills,
                                      rules_version, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    @_locked
    def set_role_facts(
        self,
        posting_id: int,
        *,
        reader: str,
        prompt_version: int,
        content_hash: str,
        facts: dict[str, Any],
        seconds: float | None = None,
    ) -> None:
        """Store what a model read in a posting, replacing any earlier reading."""
        self.connection.execute(
            """
            INSERT INTO role_facts (posting_id, reader, prompt_version, content_hash,
                                    facts_json, seconds, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (posting_id) DO UPDATE
               SET reader = excluded.reader, prompt_version = excluded.prompt_version,
                   content_hash = excluded.content_hash, facts_json = excluded.facts_json,
                   seconds = excluded.seconds, updated_at = excluded.updated_at
            """,
            (posting_id, reader, prompt_version, content_hash,
             json.dumps(facts, ensure_ascii=False), seconds, utcnow()),
        )
        self.connection.commit()

    def _posting_by_platform_job_id(self, posting: Posting) -> sqlite3.Row | None:
        """An earlier copy of a LinkedIn / Indeed / Amazon job, under whatever company.

        Their job ids are unique site-wide, but the company slug is derived from
        the name read off the page -- which can be missing or change between
        reads. Matching on the id alone keeps one row per job.
        """
        if posting.platform not in _SITE_WIDE_JOB_IDS:
            return None
        return self.connection.execute(
            """
            SELECT p.id, p.company_id FROM postings p
              JOIN companies co ON co.id = p.company_id
             WHERE co.platform = ? AND p.job_id = ?
            """,
            (str(posting.platform), posting.job_id),
        ).fetchone()

    @_locked
    def _upsert_company(
        self, platform: str, slug: str, name: str | None, now: str
    ) -> int:
        row = self.connection.execute(
            "SELECT id, name FROM companies WHERE platform = ? AND slug = ?",
            (platform, slug),
        ).fetchone()

        if row is None:
            cursor = self.connection.execute(
                """
                INSERT INTO companies (platform, slug, name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                """,
                (platform, slug, name, now, now),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

        self.connection.execute(
            "UPDATE companies SET last_seen = ?, name = COALESCE(?, name) WHERE id = ?",
            (now, name, row["id"]),
        )
        self.connection.commit()
        return int(row["id"])

    # -- reading ----------------------------------------------------------

    @_locked
    def resumes(self) -> list[dict[str, Any]]:
        """Every resume, newest first, without its file. `used`: marks pointing at it."""
        return [
            _row(r)
            for r in self.connection.execute(
                f"SELECT {_RESUME_COLUMNS} FROM resumes r ORDER BY r.uploaded_at DESC, r.id DESC"
            )
        ]

    @_locked
    def resume_results(self) -> dict[int | None, dict[str, int]]:
        """What came of the applications each resume went to; None: sent without one.

        `sent` applications, of which `waiting` have no step yet. `oa`,
        `interview`, `offer` and `rejected` count the applications that
        reached that step, so one rejected after an interview is in both;
        `replied` counts those that reached anything but a rejection.
        """
        reached = "EXISTS (SELECT 1 FROM application_events ae WHERE ae.posting_id = ap.posting_id{})"
        steps = ", ".join(
            f"SUM({reached.format(f" AND ae.stage = '{stage}'")}) AS {stage}" for stage in Outcome
        )
        replied = reached.format(f" AND ae.stage != '{Outcome.REJECTED}'")
        rows = self.connection.execute(
            f"""
            SELECT ap.resume_id, COUNT(*) AS sent,
                   SUM(NOT {reached.format("")}) AS waiting,
                   {steps},
                   SUM({replied}) AS replied
              FROM applications ap
             WHERE ap.status = '{AppStatus.APPLIED}'
             GROUP BY ap.resume_id
            """
        )
        return {
            r["resume_id"]: {key: r[key] for key in r.keys() if key != "resume_id"} for r in rows
        }

    @_locked
    def resume_file(self, resume_id: int) -> dict[str, Any] | None:
        """A resume's file: filename, content_type and data (bytes)."""
        row = self.connection.execute(
            "SELECT filename, content_type, data FROM resumes WHERE id = ?", (resume_id,)
        ).fetchone()
        return None if row is None else {**_row(row), "data": bytes(row["data"])}

    def _resume(self, resume_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            f"SELECT {_RESUME_COLUMNS} FROM resumes r WHERE r.id = ?", (resume_id,)
        ).fetchone()
        return None if row is None else _row(row)

    @_locked
    def postings(
        self,
        *,
        classifier: str = "rules",
        limit: int = 200,
        offset: int = 0,
        order: str = "newest",
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """Rows for the UI table. `filters` are PostingFilter's fields.

        `order` is "newest" (last fetched first), "posted" (latest posting
        date first), "posted_asc" (earliest first; undated last either way) or
        "deadline": postings closing soon first, soonest apply-by date first,
        then the rest newest first.
        """
        where, params = PostingFilter(**filters).where(classifier)
        first, first_params = "", []
        if order == "deadline":
            soon, first_params = closing_soon_sql()
            first = f"CASE WHEN {soon} THEN apply_by ELSE '9999' END, "
        elif order == "posted":
            first = "posted_on IS NULL, posted_on DESC, "
        elif order == "posted_asc":
            first = "posted_on IS NULL, posted_on ASC, "
        sql = f"""
            SELECT * FROM posting_overview
             WHERE {where}
             ORDER BY {first}last_fetched DESC, posting_id DESC
             LIMIT ? OFFSET ?
        """
        return [
            _row(r)
            for r in self.connection.execute(sql, [*params, *first_params, limit, offset])
        ]

    @_locked
    def day_counts(
        self, kind: str, start: str, end: str, *, classifier: str = "rules", **filters: Any
    ) -> dict[str, int]:
        """Postings per local day from `start` to `end` (YYYY-MM-DD, inclusive).

        `kind` "applied" counts postings marked applied, on the day marked;
        "added" counts postings on the day first seen. `filters` narrow both,
        as for `postings`. Days with none are left out.
        """
        day = _LOCAL_DAY[kind]
        where, params = PostingFilter(**filters).where(classifier)
        if kind == "applied":
            where += f" AND app_status = '{AppStatus.APPLIED}'"
        rows = self.connection.execute(
            f"""
            SELECT {day} AS day, COUNT(*) AS n FROM posting_overview
             WHERE {where} AND {day} BETWEEN ? AND ?
             GROUP BY 1
            """,
            [*params, start, end],
        )
        return {row["day"]: row["n"] for row in rows}

    @_locked
    def count_postings(self, *, classifier: str = "rules", **filters: Any) -> int:
        """How many rows `postings` would return with no limit: the page total."""
        where, params = PostingFilter(**filters).where(classifier)
        return self._scalar(
            f"SELECT COUNT(*) FROM posting_overview WHERE {where}", params
        )

    @_locked
    def filtered_label_counts(
        self, *, classifier: str = "rules", **filters: Any
    ) -> dict[str, int]:
        """Label counts under every filter except the label one.

        What the UI shows beside each label checkbox: how many rows ticking it
        would add, given the companies and search already applied.
        """
        where, params = replace(PostingFilter(**filters), label=None).where(classifier)
        rows = self.connection.execute(
            f"""
            SELECT label, COUNT(*) AS n FROM posting_overview
             WHERE {where} AND label IS NOT NULL
             GROUP BY label
            """,
            params,
        )
        counts = {str(label): 0 for label in Label}
        counts.update({r["label"]: r["n"] for r in rows})
        return counts

    @_locked
    def filtered_region_counts(
        self, *, classifier: str = "rules", **filters: Any
    ) -> dict[str, int]:
        """Region counts under every filter except the region one.

        A posting listing both Austin and London counts in both regions.
        """
        where, params = replace(PostingFilter(**filters), region=None).where(classifier)
        sums = ", ".join(
            f"COALESCE(SUM(jf_in_region(location, '{region}')), 0)" for region in Region
        )
        row = self.connection.execute(
            f"SELECT {sums} FROM posting_overview WHERE {where}", params
        ).fetchone()
        return {str(region): int(n) for region, n in zip(Region, row)}

    @_locked
    def filtered_app_counts(
        self, *, classifier: str = "rules", **filters: Any
    ) -> dict[str, int]:
        """Postings per application status, "none" for unmarked, under every other filter."""
        where, params = replace(PostingFilter(**filters), app_status=None).where(classifier)
        rows = self.connection.execute(
            f"""
            SELECT COALESCE(app_status, '{NO_STATUS}') AS status, COUNT(*) AS n
              FROM posting_overview WHERE {where} GROUP BY 1
            """,
            params,
        )
        counts = {NO_STATUS: 0, **{str(status): 0 for status in AppStatus}}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts

    @_locked
    def filtered_outcome_counts(
        self, *, classifier: str = "rules", **filters: Any
    ) -> dict[str, int]:
        """Applied postings by where they stand, "waiting" for no step yet.

        Under every filter but the outcome one. Each application is counted
        once, so the counts add up to the applied postings the filters leave.
        """
        where, params = replace(PostingFilter(**filters), outcome=None).where(classifier)
        rows = self.connection.execute(
            f"""
            SELECT COALESCE(outcome, '{WAITING}') AS outcome, COUNT(*) AS n
              FROM posting_overview
             WHERE {where} AND app_status = '{AppStatus.APPLIED}' GROUP BY 1
            """,
            params,
        )
        counts = {WAITING: 0, **{str(stage): 0 for stage in Outcome}}
        counts.update({r["outcome"]: r["n"] for r in rows})
        return counts

    @_locked
    def filtered_stage_counts(
        self, *, classifier: str = "rules", **filters: Any
    ) -> dict[str, int]:
        """Postings per funnel stage, "unscreened" for none, under every other filter.

        Only stages that occur are listed: the stages belong to jobfilter.roles.
        """
        where, params = replace(PostingFilter(**filters), stage=None).where(classifier)
        rows = self.connection.execute(
            f"""
            SELECT COALESCE(screen_stage, '{UNSCREENED}') AS stage, COUNT(*) AS n
              FROM posting_overview WHERE {where} GROUP BY 1
            """,
            params,
        )
        return {r["stage"]: r["n"] for r in rows}

    @_locked
    def filtered_verdict_counts(
        self, *, classifier: str = "rules", **filters: Any
    ) -> dict[str, int]:
        """Postings per model verdict, "unread" for none, under every other filter."""
        where, params = replace(PostingFilter(**filters), verdict=None).where(classifier)
        rows = self.connection.execute(
            f"""
            SELECT COALESCE(verdict, '{UNREAD}') AS verdict, COUNT(*) AS n
              FROM posting_overview WHERE {where} GROUP BY 1
            """,
            params,
        )
        return {r["verdict"]: r["n"] for r in rows}

    @_locked
    def role_facts(self) -> dict[int, dict[str, Any]]:
        """posting id -> its stored reading: reader, prompt_version, content_hash, facts."""
        return {
            r["posting_id"]: {
                "reader": r["reader"],
                "prompt_version": r["prompt_version"],
                "content_hash": r["content_hash"],
                "facts": json.loads(r["facts_json"]),
            }
            for r in self.connection.execute(
                "SELECT posting_id, reader, prompt_version, content_hash, facts_json FROM role_facts"
            )
        }

    @_locked
    def screen_inputs(self, classifier: str = "rules") -> list[dict[str, Any]]:
        """What the role funnel reads of every stored posting, oldest first."""
        rows = self.connection.execute(
            """
            SELECT p.id AS posting_id, co.slug AS company_slug, p.title, p.location,
                   p.description_text, p.content_hash,
                   (SELECT label FROM classifications c
                     WHERE c.posting_id = p.id AND c.classifier_name = ?
                     ORDER BY c.id DESC LIMIT 1) AS label
              FROM postings p JOIN companies co ON co.id = p.company_id
             ORDER BY p.id
            """,
            (classifier,),
        )
        return [_row(r) for r in rows]

    @_locked
    def location_places(self, classifier: str = "rules") -> list[dict[str, Any]]:
        """Every distinct place postings list, with how many postings list it.

        A multi-place location ("Austin, TX; London") contributes to each of
        its places. Most-listed first: the order the location finder offers.
        """
        where, params = PostingFilter().where(classifier)
        totals: dict[str, int] = {}
        for row in self.connection.execute(
            f"""
            SELECT location, COUNT(*) AS n FROM posting_overview
             WHERE {where} AND location IS NOT NULL
             GROUP BY location
            """,
            params,
        ):
            for place in set(split_places(row["location"])):
                totals[place] = totals.get(place, 0) + row["n"]
        return [
            {"place": place, "total": n}
            for place, n in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    @_locked
    def board_companies(self, platforms: Iterable[Platform]) -> list[BoardCompany]:
        """Stored companies on these platforms that hold a posting, by slug.

        What a refresh walks. The caller names the platforms: only it knows
        which ones have a board to re-read (LinkedIn and Indeed do not).
        """
        wanted = [str(platform) for platform in platforms]
        if not wanted:
            return []
        rows = self.connection.execute(
            f"""
            SELECT co.id, co.platform, co.slug, co.name FROM companies co
             WHERE co.platform IN ({", ".join("?" * len(wanted))})
               AND EXISTS (SELECT 1 FROM postings p WHERE p.company_id = co.id)
             ORDER BY co.slug COLLATE NOCASE, co.platform
            """,
            wanted,
        )
        return [
            BoardCompany(int(r["id"]), Platform(r["platform"]), r["slug"], r["name"])
            for r in rows
        ]

    @_locked
    def company_jobs(
        self, company_id: int, classifier: str = "rules"
    ) -> dict[str, StoredJob]:
        """job id -> what is stored for it, for one company.

        Deliberately without the description or raw JSON: a refresh compares
        hashes, and a large board holds thousands of postings.
        """
        rows = self.connection.execute(
            """
            SELECT p.id, p.job_id, p.url, p.content_hash, p.closed_at,
                   (SELECT label FROM classifications c
                     WHERE c.posting_id = p.id AND c.classifier_name = ?
                     ORDER BY c.id DESC LIMIT 1) AS label
              FROM postings p WHERE p.company_id = ?
            """,
            (classifier, company_id),
        )
        return {
            r["job_id"]: StoredJob(
                int(r["id"]), r["url"], r["content_hash"], r["label"], r["closed_at"]
            )
            for r in rows
        }

    @_locked
    def refresh_summary(self) -> dict[int, dict[str, Any]]:
        """company id -> its latest board refresh and what that one changed."""
        summary: dict[int, dict[str, Any]] = {}
        runs: dict[int, int] = {}
        for row in self.connection.execute(
            """
            SELECT br.company_id, br.run_id, br.status, br.listed, br.error,
                   br.created_at
              FROM board_refreshes br
             WHERE br.id = (SELECT MAX(x.id) FROM board_refreshes x
                             WHERE x.company_id = br.company_id)
            """
        ):
            runs[row["company_id"]] = row["run_id"]
            summary[row["company_id"]] = {
                "at": row["created_at"],
                "status": row["status"],
                "listed": row["listed"],
                "error": row["error"],
                "changes": {},
                "changed": 0,
            }
        for row in self.connection.execute(
            """
            SELECT p.company_id, pc.run_id, pc.kind, COUNT(*) AS n
              FROM posting_changes pc JOIN postings p ON p.id = pc.posting_id
             GROUP BY p.company_id, pc.run_id, pc.kind
            """
        ):
            if runs.get(row["company_id"]) != row["run_id"]:
                continue
            entry = summary[row["company_id"]]
            entry["changes"][row["kind"]] = row["n"]
            entry["changed"] += row["n"]
        return summary

    @_locked
    def stored_postings(
        self, classifier: str = "rules"
    ) -> list[tuple[int, Posting, str | None]]:
        """Every stored posting, rebuilt, with its current label.

        What `reclassify` needs to run new rules over old postings without
        fetching them again.
        """
        rows = self.connection.execute(
            """
            SELECT p.id, co.platform, co.slug, p.job_id, p.title, p.location, p.url,
                   p.description_text, p.raw_json, p.posted_ago, p.applicants,
                   (SELECT label FROM classifications c
                     WHERE c.posting_id = p.id AND c.classifier_name = ?
                     ORDER BY c.id DESC LIMIT 1) AS label
              FROM postings p JOIN companies co ON co.id = p.company_id
             ORDER BY p.id
            """,
            (classifier,),
        )
        return [
            (
                int(r["id"]),
                Posting(
                    platform=Platform(r["platform"]),
                    company_slug=r["slug"],
                    job_id=r["job_id"],
                    title=r["title"] or "",
                    location=r["location"],
                    url=r["url"],
                    description_text=r["description_text"] or "",
                    raw=json.loads(r["raw_json"] or "{}"),
                    posted_ago=r["posted_ago"],
                    applicants=r["applicants"],
                ),
                r["label"],
            )
            for r in rows
        ]

    @_locked
    def posting(self, posting_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM posting_overview WHERE posting_id = ?", (posting_id,)
        ).fetchone()
        if row is None:
            return None
        record = _row(row)
        record["description_text"] = self.connection.execute(
            "SELECT description_text FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()["description_text"]
        record["history"] = self.history(posting_id)
        return record

    @_locked
    def history(self, posting_id: int) -> list[dict[str, Any]]:
        """Every classification ever recorded for a posting, oldest first."""
        return [
            _row(r)
            for r in self.connection.execute(
                """
                SELECT id, classifier_name, rules_version, label, confidence,
                       evidence, matched_rule, matched_rules_json, content_hash,
                       created_at
                  FROM classifications
                 WHERE posting_id = ?
                 ORDER BY id
                """,
                (posting_id,),
            )
        ]

    @_locked
    def company_summary(
        self, classifier: str = "rules", **filters: Any
    ) -> list[dict[str, Any]]:
        """Per company: totals and how many of its postings are restricted.

        `filters` are PostingFilter's fields. With any set, the counts cover
        only the matching postings, while every company stays listed (at zero
        if nothing of its matches) and `all_total` keeps its unfiltered total.
        The company filters themselves are ignored: this table is per company.
        """
        rows = self._company_counts(classifier, PostingFilter())
        wanted = replace(PostingFilter(**filters), company=None, exclude_companies=None)
        if wanted != PostingFilter():
            matching = {
                (r["company_slug"], r["platform"]): r
                for r in self._company_counts(classifier, wanted)
            }
            rows = [
                {
                    **row,
                    **{
                        key: matching.get((row["company_slug"], row["platform"]), {}).get(key, 0)
                        for key in _COMPANY_COUNTS
                    },
                    "all_total": row["total"],
                }
                for row in rows
            ]
            rows.sort(key=lambda r: (-r["restricted"], -r["total"], r["company_slug"]))
        else:
            rows = [{**row, "all_total": row["total"]} for row in rows]

        # `refresh` is the company's latest board refresh, or None if it
        # never had one (every LinkedIn / Indeed company, for a start).
        refreshes = self.refresh_summary()
        return [{**row, "refresh": refreshes.get(row["company_id"])} for row in rows]

    def _company_counts(
        self, classifier: str, wanted: PostingFilter
    ) -> list[dict[str, Any]]:
        where, params = wanted.where(classifier)
        rows = self.connection.execute(
            f"""
            SELECT company_slug, platform, MAX(company_name) AS company_name,
                   MAX(company_id) AS company_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN label LIKE 'RESTRICTED%' THEN 1 ELSE 0 END) AS restricted,
                   SUM(CASE WHEN label = 'UNCLEAR' THEN 1 ELSE 0 END) AS unclear,
                   SUM(CASE WHEN label = 'INFO_NO_SPONSORSHIP' THEN 1 ELSE 0 END) AS info,
                   SUM(CASE WHEN label = 'OPEN' THEN 1 ELSE 0 END) AS open_count,
                   SUM(CASE WHEN closed_at IS NOT NULL THEN 1 ELSE 0 END) AS closed,
                   MAX(last_fetched) AS last_fetched
              FROM posting_overview
             WHERE {where}
             GROUP BY company_slug, platform
             ORDER BY restricted DESC, total DESC, company_slug
            """,
            params,
        )
        return [_row(r) for r in rows]

    @_locked
    def label_counts(self, classifier: str = "rules") -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT label, COUNT(*) AS n FROM posting_overview
             WHERE (classifier_name IS NULL OR classifier_name = ?) AND label IS NOT NULL
             GROUP BY label
            """,
            (classifier,),
        )
        counts = {str(label): 0 for label in Label}
        counts.update({r["label"]: r["n"] for r in rows})
        return counts

    @_locked
    def stats(self, classifier: str = "rules") -> dict[str, Any]:
        counts = self.label_counts(classifier)
        return {
            "postings": self._scalar("SELECT COUNT(*) FROM postings"),
            "companies": self._scalar("SELECT COUNT(*) FROM companies"),
            "runs": self._scalar("SELECT COUNT(*) FROM runs"),
            "errors": self._scalar(
                "SELECT COUNT(*) FROM run_items WHERE status != 'ok'"
            ),
            "labels": counts,
            "restricted": sum(
                n for label, n in counts.items() if label.startswith("RESTRICTED")
            ),
        }

    @_locked
    def companies(self) -> list[str]:
        return [
            r["slug"]
            for r in self.connection.execute(
                "SELECT slug FROM companies ORDER BY slug"
            )
        ]

    @_locked
    def failures(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent URLs that never made it to a classification.

        A failure is left out once its URL has been fetched since: a board
        retried after being rate limited should not keep listing every job.
        """
        return [
            _row(r)
            for r in self.connection.execute(
                """
                SELECT r.url_as_given, r.status, r.origin, r.error_type, r.error,
                       r.created_at
                  FROM run_items r
                  LEFT JOIN postings p
                    ON p.url = r.url_as_given AND p.last_fetched >= r.created_at
                 WHERE r.status != 'ok' AND p.id IS NULL
                 ORDER BY r.id DESC LIMIT ?
                """,
                (limit,),
            )
        ]

    @_locked
    def _scalar(self, sql: str, params: Iterable[Any] = ()) -> int:
        return int(self.connection.execute(sql, tuple(params)).fetchone()[0])


@dataclass(frozen=True, slots=True)
class BoardCompany:
    """A stored company whose board can be re-read."""

    company_id: int
    platform: Platform
    slug: str
    name: str | None


@dataclass(frozen=True, slots=True)
class StoredJob:
    """What a refresh compares a freshly fetched job against."""

    posting_id: int
    url: str
    content_hash: str
    label: str | None
    closed_at: str | None


@dataclass(frozen=True, slots=True)
class RoleScreen:
    """Where the role funnel stopped one posting, as stored."""

    posting_id: int
    stage: str
    reason: str
    evidence: str | None = None
    category: str | None = None
    rule_id: str | None = None
    focus_by: str | None = None
    focus_evidence: str | None = None
    verdict: str | None = None
    verdict_reason: str | None = None
    match_score: int = 0
    match_skills: Sequence[str] = ()


# A resume without its file, and how many marks point at it.
_RESUME_COLUMNS = """
    r.id, r.name, r.filename, r.content_type, r.sha256, r.size, r.uploaded_at,
    (SELECT COUNT(*) FROM applications ap WHERE ap.resume_id = r.id) AS used
"""

def closing_soon_sql(today: date | None = None) -> tuple[str, list[str]]:
    """SQL true for an open posting closing soon, with its parameters.

    Its apply-by date is within CLOSING_SOON_DAYS ahead, or, for a minimum
    ("at least until"), within as many days behind: past that date the posting
    can come down any day. `today` is local: a deadline is a day where you are.
    """
    today = today or date.today()
    soon = timedelta(days=CLOSING_SOON_DAYS)
    sql = (
        "(closed_at IS NULL AND (apply_by BETWEEN ? AND ?"
        " OR (apply_by_firm = 0 AND apply_by BETWEEN ? AND ?)))"
    )
    return sql, [
        today.isoformat(), (today + soon).isoformat(),
        (today - soon).isoformat(), today.isoformat(),
    ]


# The local day a posting was applied to (while marked applied: the mark's
# time does not move for a note) and first seen. Timestamps are stored in UTC.
_LOCAL_DAY = {
    "applied": "date(app_updated_at, 'localtime')",
    "added": "date(first_seen, 'localtime')",
}
CALENDAR_KINDS = tuple(_LOCAL_DAY)


# One value, several, or none: how the UI's filters arrive.
Filter = str | Sequence[str] | None


def _values(value: Filter) -> list[str]:
    """Normalize a filter to its non-blank values."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    return [item.strip() for item in items if item and item.strip()]


@dataclass(frozen=True, slots=True)
class PostingFilter:
    """Everything the postings table can be narrowed by.

    Within one field, values are alternatives (any matches); across fields,
    every field must hold. `exclude_*` fields remove matches. `place` is
    matched by jobfilter.location.place_matches: "USA" finds every US
    location, "California" finds "San Francisco Bay Area", and other text
    matches anywhere, so "Abu Dhabi" finds "Abu Dhabi, United Arab Emirates".
    """

    label: Filter = None
    company: Filter = None
    exclude_companies: Filter = None
    region: Filter = None
    place: Filter = None
    exclude_places: Filter = None
    query: str | None = None
    # Only postings their company's latest board refresh found different.
    changed: bool = False
    # Only open postings whose apply-by date is near: see CLOSING_SOON_DAYS.
    closing: bool = False
    # Only postings that went up in the last this many days (0 is today).
    posted_within: int | None = None
    # Only postings marked applied on / first seen on this local day (YYYY-MM-DD).
    applied_on: str | None = None
    added_on: str | None = None
    # applied / skipped / maybe, or "none" for postings not marked yet.
    app_status: Filter = None
    # Role funnel stages, or "unscreened" for postings not placed yet.
    stage: Filter = None
    # Model verdicts (fit / stretch / skip / unknown), or "unread" for none.
    verdict: Filter = None
    # Where an application stands (oa / interview / offer / rejected), or
    # "waiting" for an applied posting with no step yet.
    outcome: Filter = None
    # Resume ids, or "none" for postings with no resume attached.
    resume: Filter = None

    def where(self, classifier: str) -> tuple[str, list[Any]]:
        """The WHERE clause shared by the page query, its count and its facets."""
        where = ["(classifier_name IS NULL OR classifier_name = ?)"]
        params: list[Any] = [classifier]

        for column, values, op in (
            ("label", _values(self.label), "IN"),
            ("company_slug", _values(self.company), "IN"),
            ("company_slug", _values(self.exclude_companies), "NOT IN"),
        ):
            if values:
                where.append(f"{column} {op} ({', '.join('?' * len(values))})")
                params.extend(values)

        regions = _values(self.region)
        if regions:
            where.append(
                "(" + " OR ".join("jf_in_region(location, ?)" for _ in regions) + ")"
            )
            params.extend(regions)

        places = _values(self.place)
        if places:
            where.append(
                "(" + " OR ".join("jf_place_matches(location, ?)" for _ in places) + ")"
            )
            params.extend(places)

        for place in _values(self.exclude_places):
            where.append("NOT jf_place_matches(location, ?)")
            params.append(place)

        if self.query:
            where.append(
                "(title LIKE ? OR company_slug LIKE ? OR company_name LIKE ?"
                " OR location LIKE ? OR evidence LIKE ?)"
            )
            params.extend([f"%{self.query}%"] * 5)

        if self.changed:
            where.append("changes IS NOT NULL")

        if self.closing:
            soon, soon_params = closing_soon_sql()
            where.append(soon)
            params.extend(soon_params)

        if self.applied_on:
            where.append(f"(app_status = '{AppStatus.APPLIED}' AND {_LOCAL_DAY['applied']} = ?)")
            params.append(self.applied_on)

        if self.added_on:
            where.append(f"{_LOCAL_DAY['added']} = ?")
            params.append(self.added_on)

        if self.posted_within is not None:
            # Local dates, like the posting dates themselves.
            where.append("posted_on >= ?")
            params.append((date.today() - timedelta(days=self.posted_within)).isoformat())

        for column, values, missing in (
            ("app_status", _values(self.app_status), NO_STATUS),
            ("screen_stage", _values(self.stage), UNSCREENED),
            ("verdict", _values(self.verdict), UNREAD),
        ):
            if not values:
                continue
            named = [v for v in values if v != missing]
            either = []
            if named:
                either.append(f"{column} IN ({', '.join('?' * len(named))})")
                params.extend(named)
            if missing in values:
                either.append(f"{column} IS NULL")
            where.append("(" + " OR ".join(either) + ")")

        outcomes = _values(self.outcome)
        if outcomes:
            named = [v for v in outcomes if v != WAITING]
            either = []
            if named:
                either.append(f"outcome IN ({', '.join('?' * len(named))})")
                params.extend(named)
            if WAITING in outcomes:
                # Unmarked postings have no outcome either: waiting is applied only.
                either.append(f"(outcome IS NULL AND app_status = '{AppStatus.APPLIED}')")
            where.append("(" + " OR ".join(either) + ")")

        resumes = _values(self.resume)
        if resumes:
            named = [int(v) if v.isdigit() else v for v in resumes if v != NO_RESUME]
            either = []
            if named:
                either.append(f"resume_id IN ({', '.join('?' * len(named))})")
                params.extend(named)
            if NO_RESUME in resumes:
                either.append("resume_id IS NULL")
            where.append("(" + " OR ".join(either) + ")")

        return " AND ".join(where), params


# company_summary's per-company counts: the fields a filter narrows.
_COMPANY_COUNTS = ("total", "restricted", "unclear", "info", "open_count", "closed")

# Platforms whose job ids identify a posting without the company.
_SITE_WIDE_JOB_IDS = frozenset({Platform.LINKEDIN, Platform.INDEED, Platform.AMAZON})


def company_name(posting: Posting) -> str | None:
    """A human-readable company name when the platform gave us one.

    Greenhouse does, and so does the browser extension (it reads the name off
    the page); so do Dover (`client_name`) and Rippling (`companyName`).
    Ashby, Lever and Workday expose the slug alone. Reading it out of `raw`
    here keeps Posting normalized across platforms while still surfacing the
    nicer name in the UI.
    """
    for key in _NAME_KEYS:
        name = posting.raw.get(key)
        if isinstance(name, str) and name.strip():
            return name
    # SmartRecruiters nests it: {"company": {"name": "Intuitive"}}.
    company = posting.raw.get("company")
    if isinstance(company, dict) and isinstance(company.get("name"), str) and company["name"].strip():
        return company["name"]
    return None


# Where each platform's raw JSON keeps the company's display name.
_NAME_KEYS = ("company_name", "client_name", "companyName")


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
