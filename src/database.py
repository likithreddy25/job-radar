"""SQLite-backed state management replacing the original JSON file approach.

Schema:
  jobs     — every job ever seen, with score/label and timestamps
  boards   — ATS board registry with health tracking
  cursors  — pagination state (replaces boards_cursor.json)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

try:
    import libsql  # type: ignore
except ImportError:
    libsql = None

from .job_intelligence import (
    description_similarity,
    extract_workday_req_id,
    extract_structured_fields,
    make_canonical_key,
    normalize_compare_url,
    normalize_text_encoding,
    score_employer_quality,
    to_structured_json,
)
from .scoring_policy import label_for_score

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _score_to_grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 55:
        return "D"
    if score >= 40:
        return "E"
    return "F"


def _normalize_job_row(row: dict[str, Any]) -> dict[str, Any]:
    try:
        score = max(0, min(int(row.get("score") or 0), 100))
    except (TypeError, ValueError):
        score = 0
    row["score"] = score
    row["grade"] = _score_to_grade(score)
    label = str(row.get("label") or "").strip().lower()
    if label not in {"yes", "maybe", "no"}:
        row["label"] = label_for_score(score)
    return row


class _CompatCursor:
    """Wrap libsql tuple rows into dict-like rows keyed by column name."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def _normalize_row(self, row: Any) -> Any:
        if row is None or isinstance(row, dict) or isinstance(row, sqlite3.Row):
            return row
        if not isinstance(row, tuple):
            return row
        description = getattr(self._cursor, "description", None) or []
        column_names = [str(col[0]) for col in description if col]
        if len(column_names) == len(row):
            return {column_names[idx]: value for idx, value in enumerate(row)}
        return row

    def fetchone(self) -> Any:
        return self._normalize_row(self._cursor.fetchone())

    def fetchall(self) -> list[Any]:
        return [self._normalize_row(row) for row in self._cursor.fetchall()]


class _CompatConnection:
    """Provide sqlite3-like row behavior for libsql connections."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def execute(self, *args, **kwargs) -> _CompatCursor:
        return _CompatCursor(self._conn.execute(*args, **kwargs))

    def cursor(self, *args, **kwargs) -> _CompatCursor:
        return _CompatCursor(self._conn.cursor(*args, **kwargs))


CREATE_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    key          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    provenance   TEXT NOT NULL DEFAULT 'local',
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    location     TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    posted       TEXT NOT NULL DEFAULT '',
    score        INTEGER NOT NULL DEFAULT 0,
    label        TEXT NOT NULL DEFAULT 'no',
    grade        TEXT NOT NULL DEFAULT 'F',
    evaluation_json TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    manual_input INTEGER NOT NULL DEFAULT 0,
    fit_summary  TEXT NOT NULL DEFAULT '',
    canonical_key TEXT NOT NULL DEFAULT '',
    structured_json TEXT NOT NULL DEFAULT '',
    is_repost INTEGER NOT NULL DEFAULT 0,
    repost_of_key TEXT NOT NULL DEFAULT '',
    employer_quality_score INTEGER NOT NULL DEFAULT 50,
    employer_quality_reason TEXT NOT NULL DEFAULT '',
    pipeline_status TEXT NOT NULL DEFAULT 'new',
    pipeline_notes TEXT NOT NULL DEFAULT '',
    follow_up_date TEXT NOT NULL DEFAULT '',
    pipeline_updated_at TEXT NOT NULL DEFAULT '',
    viewed_at    TEXT NOT NULL DEFAULT '',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boards (
    board_id     TEXT PRIMARY KEY,
    platform     TEXT NOT NULL,
    company      TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    last_checked TEXT,
    job_count    INTEGER NOT NULL DEFAULT 0,
    fail_count   INTEGER NOT NULL DEFAULT 0,
    fail_reason  TEXT NOT NULL DEFAULT '',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cursors (
    name   TEXT PRIMARY KEY,
    value  TEXT NOT NULL DEFAULT '0'
);

CREATE TABLE IF NOT EXISTS generated_resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key     TEXT NOT NULL,
    job_title   TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    format      TEXT NOT NULL DEFAULT 'markdown',
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generated_artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id   INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    format      TEXT NOT NULL DEFAULT 'text',
    filename    TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY(resume_id) REFERENCES generated_resumes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_flags (
    name        TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key     TEXT NOT NULL,
    action      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key    TEXT NOT NULL,
    entity_type   TEXT NOT NULL DEFAULT 'main',
    mode          TEXT NOT NULL DEFAULT '',
    platform      TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'success',
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    new_count     INTEGER NOT NULL DEFAULT 0,
    yes_count     INTEGER NOT NULL DEFAULT 0,
    maybe_count   INTEGER NOT NULL DEFAULT 0,
    stale_count   INTEGER NOT NULL DEFAULT 0,
    jd_coverage   REAL NOT NULL DEFAULT 0,
    error_text    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_label  ON jobs(label);
CREATE INDEX IF NOT EXISTS idx_boards_platform ON boards(platform);
CREATE INDEX IF NOT EXISTS idx_boards_status   ON boards(status);
CREATE INDEX IF NOT EXISTS idx_generated_resumes_job_key ON generated_resumes(job_key);
CREATE INDEX IF NOT EXISTS idx_generated_artifacts_resume_id ON generated_artifacts(resume_id);
CREATE INDEX IF NOT EXISTS idx_feedback_job_key ON feedback(job_key);
CREATE INDEX IF NOT EXISTS idx_feedback_action ON feedback(action);
CREATE INDEX IF NOT EXISTS idx_source_runs_source_key ON source_runs(source_key);
CREATE INDEX IF NOT EXISTS idx_source_runs_finished_at ON source_runs(finished_at);
"""


class Database:
    def __init__(
        self,
        path: str,
        *,
        turso_url: str = "",
        turso_auth_token: str = "",
        turso_sync_interval_seconds: int = 15,
    ) -> None:
        self.path = path
        self.turso_url = (turso_url or "").strip()
        self.turso_auth_token = (turso_auth_token or "").strip()
        self._uses_turso = bool(self.turso_url)
        self._sync_interval_seconds = max(int(turso_sync_interval_seconds or 0), 0)
        self._last_sync_monotonic = 0.0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        raw_conn = self._connect(path)
        if hasattr(raw_conn, "row_factory"):
            raw_conn.row_factory = sqlite3.Row
            self._conn = raw_conn
        else:
            self._conn = _CompatConnection(raw_conn)
        self._apply_schema_script(CREATE_SQL)
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema()
        self._conn.commit()
        if self._uses_turso:
            self.sync(force=True)
        log.debug("Database opened: %s", path)

    def _apply_schema_script(self, script: str) -> None:
        statements = [chunk.strip() for chunk in script.split(";") if chunk.strip()]
        for statement in statements:
            if self._uses_turso and statement.upper() == "PRAGMA JOURNAL_MODE=WAL":
                continue
            self._conn.execute(statement)

    def _connect(self, path: str) -> Any:
        if not self._uses_turso:
            return sqlite3.connect(path, check_same_thread=False, timeout=30)
        if libsql is None:
            raise RuntimeError(
                "Turso database requested, but the 'libsql' package is not installed. "
                "Install dependencies with 'pip install -r requirements.txt'."
            )
        log.info("Opening Turso-backed embedded replica at %s", path)
        kwargs: dict[str, object] = {"sync_url": self.turso_url}
        if self.turso_auth_token:
            kwargs["auth_token"] = self.turso_auth_token
        return libsql.connect(path, **kwargs)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
                if self._uses_turso:
                    self.sync(force=True)
            except Exception:
                self._conn.rollback()
                raise

    def sync(self, *, force: bool = False) -> bool:
        if not self._uses_turso or not hasattr(self._conn, "sync"):
            return False
        now = time.monotonic()
        if not force and self._sync_interval_seconds > 0:
            if now - self._last_sync_monotonic < self._sync_interval_seconds:
                return False
        with self._lock:
            self._conn.sync()
            self._last_sync_monotonic = time.monotonic()
        return True

    def _ensure_schema(self) -> None:
        job_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "provenance" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN provenance TEXT NOT NULL DEFAULT 'local'")
        if "grade" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN grade TEXT NOT NULL DEFAULT 'F'")
        if "evaluation_json" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN evaluation_json TEXT NOT NULL DEFAULT ''")
        if "description" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "manual_input" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN manual_input INTEGER NOT NULL DEFAULT 0")
        if "fit_summary" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN fit_summary TEXT NOT NULL DEFAULT ''")
        if "canonical_key" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN canonical_key TEXT NOT NULL DEFAULT ''")
        if "structured_json" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN structured_json TEXT NOT NULL DEFAULT ''")
        if "is_repost" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN is_repost INTEGER NOT NULL DEFAULT 0")
        if "repost_of_key" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN repost_of_key TEXT NOT NULL DEFAULT ''")
        if "employer_quality_score" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN employer_quality_score INTEGER NOT NULL DEFAULT 50")
        if "employer_quality_reason" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN employer_quality_reason TEXT NOT NULL DEFAULT ''")
        if "pipeline_status" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN pipeline_status TEXT NOT NULL DEFAULT 'new'")
        if "pipeline_notes" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN pipeline_notes TEXT NOT NULL DEFAULT ''")
        if "follow_up_date" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN follow_up_date TEXT NOT NULL DEFAULT ''")
        if "pipeline_updated_at" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN pipeline_updated_at TEXT NOT NULL DEFAULT ''")
        if "viewed_at" not in job_columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN viewed_at TEXT NOT NULL DEFAULT ''")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_grade ON jobs(grade)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pipeline_status ON jobs(pipeline_status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_viewed_at ON jobs(viewed_at)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_canonical_key ON jobs(canonical_key)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_is_repost ON jobs(is_repost)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_source_runs_mode ON source_runs(mode)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_source_runs_status ON source_runs(status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_source_runs_entity_type ON source_runs(entity_type)")

    # -------------------------------------------------------------------------
    # Job tracking
    # -------------------------------------------------------------------------

    def is_new_job(self, key: str) -> bool:
        """Return True if this job key has never been seen before."""
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone()
        return row is None

    def mark_job_seen(
        self,
        *,
        key: str,
        source: str,
        company: str,
        title: str,
        location: str,
        url: str,
        posted: str,
        score: int,
        label: str,
        grade: str = "F",
        evaluation_json: str = "",
        description: str = "",
        manual_input: bool = False,
        fit_summary: str = "",
        canonical_key: str = "",
        structured_json: str = "",
        is_repost: bool = False,
        repost_of_key: str = "",
        employer_quality_score: int = 50,
        employer_quality_reason: str = "",
        pipeline_status: str = "new",
        pipeline_notes: str = "",
        follow_up_date: str = "",
        provenance: str = "local",
    ) -> None:
        now = _now()
        effective_posted = (posted or "").strip()
        provenance = (provenance or "local").strip() or "local"
        description = normalize_text_encoding(description)
        canonical = canonical_key or make_canonical_key(company, title)
        structured = structured_json or to_structured_json(extract_structured_fields(title, description, location=location))
        quality_score = max(0, min(int(employer_quality_score or 0), 100))
        quality_reason = employer_quality_reason or score_employer_quality(source, company, url)[1]
        repost_key = repost_of_key
        repost_flag = bool(is_repost)
        if not repost_key:
            repost_key = self._find_repost_candidate(
                key=key,
                company=company,
                canonical_key=canonical,
                description=description,
                url=url,
            )
            repost_flag = bool(repost_key)
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO jobs(
                    key,source,provenance,company,title,location,url,posted,score,label,grade,evaluation_json,description,manual_input,fit_summary,canonical_key,structured_json,is_repost,repost_of_key,employer_quality_score,employer_quality_reason,pipeline_status,pipeline_notes,follow_up_date,pipeline_updated_at,viewed_at,first_seen,last_seen
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    provenance=CASE
                        WHEN excluded.provenance = '' THEN jobs.provenance
                        WHEN jobs.provenance = '' THEN excluded.provenance
                        WHEN jobs.provenance = excluded.provenance THEN jobs.provenance
                        WHEN instr('+' || jobs.provenance || '+', '+' || excluded.provenance || '+') > 0 THEN jobs.provenance
                        ELSE jobs.provenance || '+' || excluded.provenance
                    END,
                    title=excluded.title,
                    location=excluded.location,
                    url=excluded.url,
                    posted=CASE
                        WHEN jobs.posted <> '' THEN jobs.posted
                        WHEN excluded.posted <> '' THEN excluded.posted
                        ELSE jobs.posted
                    END,
                    score=excluded.score,
                    label=excluded.label,
                    grade=excluded.grade,
                    evaluation_json=CASE
                        WHEN excluded.evaluation_json <> '' THEN excluded.evaluation_json
                        ELSE jobs.evaluation_json
                    END,
                    description=CASE
                        WHEN excluded.description <> '' THEN excluded.description
                        ELSE jobs.description
                    END,
                    manual_input=excluded.manual_input,
                    fit_summary=CASE
                        WHEN excluded.fit_summary <> '' THEN excluded.fit_summary
                        ELSE jobs.fit_summary
                    END,
                    canonical_key=CASE
                        WHEN excluded.canonical_key <> '' THEN excluded.canonical_key
                        ELSE jobs.canonical_key
                    END,
                    structured_json=CASE
                        WHEN excluded.structured_json <> '' THEN excluded.structured_json
                        ELSE jobs.structured_json
                    END,
                    is_repost=CASE
                        WHEN excluded.is_repost <> 0 THEN excluded.is_repost
                        ELSE jobs.is_repost
                    END,
                    repost_of_key=CASE
                        WHEN excluded.repost_of_key <> '' THEN excluded.repost_of_key
                        ELSE jobs.repost_of_key
                    END,
                    employer_quality_score=CASE
                        WHEN excluded.employer_quality_score <> 0 THEN excluded.employer_quality_score
                        ELSE jobs.employer_quality_score
                    END,
                    employer_quality_reason=CASE
                        WHEN excluded.employer_quality_reason <> '' THEN excluded.employer_quality_reason
                        ELSE jobs.employer_quality_reason
                    END,
                    pipeline_status=CASE
                        WHEN jobs.pipeline_status = '' THEN excluded.pipeline_status
                        ELSE jobs.pipeline_status
                    END,
                    pipeline_notes=CASE
                        WHEN jobs.pipeline_notes = '' THEN excluded.pipeline_notes
                        ELSE jobs.pipeline_notes
                    END,
                    follow_up_date=CASE
                        WHEN jobs.follow_up_date = '' THEN excluded.follow_up_date
                        ELSE jobs.follow_up_date
                    END,
                    pipeline_updated_at=CASE
                        WHEN jobs.pipeline_updated_at = '' THEN excluded.pipeline_updated_at
                        ELSE jobs.pipeline_updated_at
                    END,
                    viewed_at=CASE
                        WHEN jobs.viewed_at = '' THEN excluded.viewed_at
                        ELSE jobs.viewed_at
                    END,
                    last_seen=excluded.last_seen
                """,
                (
                    key, source, provenance, company, title, location, url, effective_posted, score, label,
                    grade, evaluation_json, description, 1 if manual_input else 0, fit_summary,
                    canonical, structured, 1 if repost_flag else 0, repost_key, quality_score, quality_reason,
                    pipeline_status, pipeline_notes, follow_up_date, now, "", now, now,
                ),
            )

    def inspect_job(
        self,
        *,
        key: str,
        source: str,
        company: str,
        title: str,
        location: str,
        url: str,
        description: str,
    ) -> dict:
        description = normalize_text_encoding(description)
        canonical_key = make_canonical_key(company, title)
        structured = extract_structured_fields(title, description, location=location)
        employer_quality_score, employer_quality_reason = score_employer_quality(source, company, url)
        repost_of_key = self._find_repost_candidate(
            key=key,
            company=company,
            canonical_key=canonical_key,
            description=description,
            url=url,
        )
        return {
            "canonical_key": canonical_key,
            "structured_json": to_structured_json(structured),
            "structured": structured,
            "is_repost": bool(repost_of_key),
            "repost_of_key": repost_of_key,
            "employer_quality_score": employer_quality_score,
            "employer_quality_reason": employer_quality_reason,
        }

    def _find_repost_candidate(
        self,
        *,
        key: str,
        company: str,
        canonical_key: str,
        description: str,
        url: str,
    ) -> str:
        with self._lock:
            return self._find_repost_candidate_locked(
                key=key,
                company=company,
                canonical_key=canonical_key,
                description=description,
                url=url,
            )

    def _find_repost_candidate_locked(
        self,
        *,
        key: str,
        company: str,
        canonical_key: str,
        description: str,
        url: str,
    ) -> str:
        if not canonical_key:
            return ""
        rows = self._conn.execute(
            """
            SELECT key, description, url, first_seen
            FROM jobs
            WHERE canonical_key=? AND lower(company)=lower(?) AND key<>?
            ORDER BY first_seen ASC
            LIMIT 25
            """,
            (canonical_key, company.strip(), key),
        ).fetchall()
        compare_url = normalize_compare_url(url)
        best_key = ""
        best_score = 0.0
        for row in rows:
            existing_url = normalize_compare_url(row["url"] or "")
            if compare_url and existing_url and compare_url == existing_url:
                return str(row["key"])
            score = description_similarity(description, row["description"] or "")
            if score > best_score:
                best_score = score
                best_key = str(row["key"])
        if best_score >= 0.88:
            return best_key
        return ""

    def source_is_bootstrapped(self, source: str) -> bool:
        """Return True if we have at least one job from this source (not first run)."""
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE source=? LIMIT 1", (source,)
        ).fetchone()
        return row is not None

    def get_seen_keys(self, source: Optional[str] = None) -> set[str]:
        """Return all seen job keys, optionally filtered by source."""
        if source:
            rows = self._conn.execute("SELECT key FROM jobs WHERE source=?", (source,)).fetchall()
        else:
            rows = self._conn.execute("SELECT key FROM jobs").fetchall()
        return {r["key"] for r in rows}

    def job_count(self, source: Optional[str] = None) -> int:
        if source:
            return self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE source=?", (source,)
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def list_jobs(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT key, source, company, title, location, url, posted, score, label, grade,
                   provenance,
                   evaluation_json, description, manual_input, fit_summary, canonical_key,
                   structured_json, is_repost, repost_of_key, employer_quality_score, employer_quality_reason, pipeline_status,
                   pipeline_notes, follow_up_date, pipeline_updated_at, viewed_at, first_seen, last_seen
            FROM jobs
            ORDER BY score DESC, employer_quality_score DESC, last_seen DESC
            LIMIT ?
            """,
            (max(limit, 1),),
        ).fetchall()
        return [_normalize_job_row(dict(r)) for r in rows]

    def list_jobs_for_board(self, limit: Optional[int] = 1000) -> list[dict]:
        query = """
            SELECT key, source, company, title, location, url, posted, score, label, grade,
                   provenance,
                   evaluation_json, description, manual_input, fit_summary, canonical_key,
                   structured_json, is_repost, repost_of_key, employer_quality_score, employer_quality_reason, pipeline_status,
                   pipeline_notes, follow_up_date, pipeline_updated_at, viewed_at, first_seen, last_seen
            FROM jobs
            ORDER BY
                CASE
                    WHEN posted IS NOT NULL AND posted <> '' THEN posted
                    ELSE first_seen
                END DESC,
                score DESC,
                employer_quality_score DESC,
                last_seen DESC
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            query += "\n            LIMIT ?"
            params = (max(limit, 1),)
        rows = self._conn.execute(query, params).fetchall()
        return [_normalize_job_row(dict(r)) for r in rows]

    def get_job(self, key: str) -> Optional[dict]:
        row = self._conn.execute(
            """
            SELECT key, source, company, title, location, url, posted, score, label, grade,
                   provenance,
                   evaluation_json, description, manual_input, fit_summary, canonical_key,
                   structured_json, is_repost, repost_of_key, employer_quality_score, employer_quality_reason, pipeline_status,
                   pipeline_notes, follow_up_date, pipeline_updated_at, viewed_at, first_seen, last_seen
            FROM jobs
            WHERE key=?
            """,
            (key,),
        ).fetchone()
        return _normalize_job_row(dict(row)) if row is not None else None

    def mark_job_viewed(self, key: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET viewed_at=CASE
                        WHEN viewed_at IS NULL OR viewed_at = '' THEN ?
                        ELSE viewed_at
                    END
                WHERE key=?
                """,
                (_now(), key),
            )

    def update_job_evaluation(
        self,
        *,
        key: str,
        score: int,
        label: str,
        grade: str,
        evaluation_json: str,
        fit_summary: str,
        description: str = "",
    ) -> None:
        description = normalize_text_encoding(description)
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET score=?,
                    label=?,
                    grade=?,
                    evaluation_json=?,
                    fit_summary=?,
                    description=CASE WHEN ? <> '' THEN ? ELSE description END,
                    last_seen=?
                WHERE key=?
                """,
                (score, label, grade, evaluation_json, fit_summary, description, description, _now(), key),
            )

    def refresh_job_intelligence(
        self,
        *,
        key: str,
        source: str,
        company: str,
        title: str,
        location: str,
        url: str,
        description: str,
    ) -> dict:
        description = normalize_text_encoding(description)
        intelligence = self.inspect_job(
            key=key,
            source=source,
            company=company,
            title=title,
            location=location,
            url=url,
            description=description,
        )
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET canonical_key=?,
                    structured_json=?,
                    is_repost=?,
                    repost_of_key=?,
                    employer_quality_score=?,
                    employer_quality_reason=?
                WHERE key=?
                """,
                (
                    intelligence["canonical_key"],
                    intelligence["structured_json"],
                    1 if intelligence["is_repost"] else 0,
                    intelligence["repost_of_key"],
                    intelligence["employer_quality_score"],
                    intelligence["employer_quality_reason"],
                    key,
                ),
            )
        return intelligence

    def get_workday_duplicate_keys(self) -> set[str]:
        rows = self._conn.execute(
            """
            SELECT key, url
            FROM jobs
            WHERE source='workday'
            """
        ).fetchall()
        by_req: dict[str, dict[str, set[str] | str]] = {}
        for row in rows:
            key = str(row["key"] or "")
            req_id = extract_workday_req_id(key) or extract_workday_req_id(str(row["url"] or ""))
            if not req_id:
                continue
            bucket = by_req.setdefault(req_id, {"url_keys": set(), "canonical_keys": set()})
            if ":url:" in key:
                bucket["url_keys"].add(key)
            else:
                bucket["canonical_keys"].add(key)
        hidden: set[str] = set()
        for bucket in by_req.values():
            if bucket["canonical_keys"] and bucket["url_keys"]:
                hidden.update(bucket["url_keys"])
        return hidden

    def update_pipeline(
        self,
        *,
        key: str,
        pipeline_status: str,
        pipeline_notes: str = "",
        follow_up_date: str = "",
    ) -> None:
        now = _now()
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET pipeline_status=?,
                    pipeline_notes=?,
                    follow_up_date=?,
                    pipeline_updated_at=?
                WHERE key=?
                """,
                (pipeline_status, pipeline_notes, follow_up_date, now, key),
            )
        feedback_action = self._feedback_action_for_pipeline(pipeline_status)
        if feedback_action:
            self.record_feedback(job_key=key, action=feedback_action, notes=f"pipeline:{pipeline_status}")

    def _feedback_action_for_pipeline(self, pipeline_status: str) -> str:
        status = (pipeline_status or "").strip().lower()
        mapping = {
            "shortlisted": "shortlisted",
            "applied": "applied",
            "interview": "interview",
            "onsite": "onsite",
            "offer": "offer",
            "screen_reject": "screen_reject",
            "ghosted": "ghosted",
            "rejected": "rejected",
            "archived": "archived",
        }
        return mapping.get(status, "")

    def record_feedback(self, *, job_key: str, action: str, notes: str = "") -> bool:
        valid_actions = {
            "applied",
            "dismissed",
            "interested",
            "shortlisted",
            "interview",
            "onsite",
            "responded",
            "screen_reject",
            "rejected",
            "offer",
            "ghosted",
            "archived",
        }
        if action not in valid_actions:
            raise ValueError(f"action must be one of {sorted(valid_actions)}, got {action!r}")
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM jobs WHERE key=?", (job_key,)).fetchone() is not None
            latest = self._conn.execute(
                "SELECT action, notes FROM feedback WHERE job_key=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (job_key,),
            ).fetchone()
        if latest is not None and str(latest["action"] or "") == action and str(latest["notes"] or "") == (notes or ""):
            return exists
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO feedback(job_key, action, created_at, notes) VALUES(?,?,?,?)",
                (job_key, action, _now(), notes or ""),
            )
        return exists

    def get_feedback_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT action, COUNT(*) as cnt FROM feedback GROUP BY action"
        ).fetchall()
        stats = {
            "applied": 0,
            "dismissed": 0,
            "interested": 0,
            "shortlisted": 0,
            "interview": 0,
            "onsite": 0,
            "offer": 0,
            "screen_reject": 0,
            "ghosted": 0,
            "rejected": 0,
            "archived": 0,
            "total": 0,
        }
        for row in rows:
            stats[str(row["action"])] = int(row["cnt"] or 0)
        stats["total"] = sum(value for key, value in stats.items() if key != "total")
        return stats

    def get_feedback_jobs(self, action: str | None = None) -> list[dict]:
        if action:
            rows = self._conn.execute(
                """
                SELECT f.job_key, f.action, f.created_at, f.notes,
                       j.company, j.title, j.url, j.score, j.label, j.source, j.pipeline_status
                FROM feedback f
                LEFT JOIN jobs j ON j.key = f.job_key
                WHERE f.action = ?
                ORDER BY f.created_at DESC, f.id DESC
                """,
                (action,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT f.job_key, f.action, f.created_at, f.notes,
                       j.company, j.title, j.url, j.score, j.label, j.source, j.pipeline_status
                FROM feedback f
                LEFT JOIN jobs j ON j.key = f.job_key
                ORDER BY f.created_at DESC, f.id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_manual_job(
        self,
        *,
        key: str,
        company: str,
        title: str,
        location: str,
        url: str,
        description: str,
        score: int,
        label: str,
        grade: str,
        evaluation_json: str,
        fit_summary: str,
    ) -> None:
        self.mark_job_seen(
            key=key,
            source="manual",
            company=company or "Manual Entry",
            title=title,
            location=location or "Unknown Location",
            url=url,
            posted="",
            score=score,
            label=label,
            grade=grade,
            evaluation_json=evaluation_json,
            description=description,
            manual_input=True,
            fit_summary=fit_summary,
        )

    # -------------------------------------------------------------------------
    # Board registry
    # -------------------------------------------------------------------------

    def is_board_dead(self, board_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM boards WHERE board_id=?", (board_id,)
            ).fetchone()
        return row is not None and row["status"] == "dead"

    def is_board_bootstrapped(self, board_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM boards WHERE board_id=?", (board_id,)
            ).fetchone()
        return row is not None

    MAX_EMPTY_BOARD_COOLDOWN_HOURS = 720  # 30 days

    @classmethod
    def empty_board_cooldown_hours(
        cls,
        fail_count: int,
        *,
        empty_fail_threshold: int = 3,
        base_cooldown_hours: int = 168,
    ) -> int:
        """Back off boards that repeatedly return zero jobs without retiring them."""
        extra = max(0, int(fail_count) - max(empty_fail_threshold, 1))
        capped_extra = min(extra, 16)
        hours = max(base_cooldown_hours, 1) * (2 ** capped_extra)
        return min(hours, cls.MAX_EMPTY_BOARD_COOLDOWN_HOURS)

    def should_skip_board(
        self,
        board_id: str,
        *,
        empty_fail_threshold: int = 3,
        broken_fail_threshold: int = 2,
        cooldown_hours: int = 168,
    ) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, fail_count, fail_reason, last_checked FROM boards WHERE board_id=?",
                (board_id,),
            ).fetchone()
        if row is None:
            return False
        status = str(row["status"] or "").strip().lower()
        fail_count = int(row["fail_count"] or 0)
        try:
            last_checked = datetime.fromisoformat(str(row["last_checked"] or ""))
        except ValueError:
            return False
        if status == "degraded":
            if fail_count < max(empty_fail_threshold, 1):
                return False
            if str(row["fail_reason"] or "").strip() != "0 jobs returned":
                return False
            effective_cooldown = self.empty_board_cooldown_hours(
                fail_count,
                empty_fail_threshold=empty_fail_threshold,
                base_cooldown_hours=cooldown_hours,
            )
            return (datetime.now(timezone.utc) - last_checked).total_seconds() < effective_cooldown * 3600
        if status == "broken":
            within_cooldown = (datetime.now(timezone.utc) - last_checked).total_seconds() < max(cooldown_hours, 1) * 3600
            return within_cooldown and fail_count >= max(broken_fail_threshold, 1)
        return False

    def was_board_checked_recently(self, board_id: str, *, cooldown_hours: int = 6) -> bool:
        if int(cooldown_hours or 0) <= 0:
            return False
        latest_run = self.get_latest_source_run(board_id, "board")
        if latest_run is not None and int(latest_run.get("new_count") or 0) > 0:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT last_checked FROM boards WHERE board_id=?",
                (board_id,),
            ).fetchone()
        if row is None:
            return False
        try:
            last_checked = datetime.fromisoformat(str(row["last_checked"] or ""))
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - last_checked).total_seconds() < max(cooldown_hours, 1) * 3600

    def upsert_board(
        self,
        *,
        board_id: str,
        platform: str,
        company: str,
        url: str,
        status: str = "active",
        job_count: int = 0,
        fail_reason: str = "",
    ) -> None:
        now = _now()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT fail_count FROM boards WHERE board_id=?", (board_id,)
            ).fetchone()
            fail_count = 0
            if existing and status in {"dead", "degraded", "broken"}:
                fail_count = existing["fail_count"] + 1
            conn.execute(
                """
                INSERT INTO boards(board_id,platform,company,url,status,last_checked,job_count,fail_count,fail_reason,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(board_id) DO UPDATE SET
                    status=excluded.status,
                    last_checked=excluded.last_checked,
                    job_count=excluded.job_count,
                    fail_count=excluded.fail_count,
                    fail_reason=excluded.fail_reason,
                    last_seen=excluded.last_seen
                """,
                (board_id, platform, company, url, status, now, job_count, fail_count, fail_reason, now, now),
            )

    def get_dead_boards(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM boards WHERE status='dead' ORDER BY board_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_board_stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        dead = self._conn.execute("SELECT COUNT(*) FROM boards WHERE status='dead'").fetchone()[0]
        broken = self._conn.execute("SELECT COUNT(*) FROM boards WHERE status='broken'").fetchone()[0]
        degraded = self._conn.execute("SELECT COUNT(*) FROM boards WHERE status='degraded'").fetchone()[0]
        active = self._conn.execute("SELECT COUNT(*) FROM boards WHERE status='active'").fetchone()[0]
        return {"total": total, "active": active, "degraded": degraded, "broken": broken, "dead": dead}

    def list_boards(self, *, limit: int = 2000, status: str = "", platform: str = "") -> list[dict]:
        sql = [
            "SELECT board_id, platform, company, url, status, last_checked, job_count, fail_count, fail_reason, first_seen, last_seen",
            "FROM boards",
        ]
        params: list[object] = []
        where: list[str] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if platform:
            where.append("platform = ?")
            params.append(platform)
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append(
            """
            ORDER BY
                CASE status
                    WHEN 'dead' THEN 0
                    WHEN 'broken' THEN 1
                    WHEN 'degraded' THEN 2
                    WHEN 'active' THEN 3
                    ELSE 3
                END,
                last_checked DESC,
                fail_count DESC,
                company ASC
            LIMIT ?
            """
        )
        params.append(max(limit, 1))
        rows = self._conn.execute("\n".join(sql), tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------------------
    # Source run history / health
    # -------------------------------------------------------------------------

    def get_latest_source_run(self, source_key: str, entity_type: str = "") -> Optional[dict]:
        sql = ["SELECT * FROM source_runs WHERE source_key=?"]
        params: list[object] = [source_key]
        if entity_type:
            sql.append("AND entity_type=?")
            params.append(entity_type)
        sql.append("ORDER BY finished_at DESC, id DESC LIMIT 1")
        row = self._conn.execute(" ".join(sql), tuple(params)).fetchone()
        return dict(row) if row is not None else None

    def record_source_run(
        self,
        *,
        source_key: str,
        entity_type: str,
        mode: str,
        platform: str = "",
        company: str = "",
        url: str = "",
        status: str,
        started_at: str,
        finished_at: str,
        latency_ms: int = 0,
        fetched_count: int = 0,
        matched_count: int = 0,
        new_count: int = 0,
        yes_count: int = 0,
        maybe_count: int = 0,
        stale_count: int = 0,
        jd_coverage: float = 0.0,
        error_text: str = "",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO source_runs(
                    source_key, entity_type, mode, platform, company, url, status,
                    started_at, finished_at, latency_ms, fetched_count, matched_count,
                    new_count, yes_count, maybe_count, stale_count, jd_coverage, error_text
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source_key,
                    entity_type,
                    mode,
                    platform,
                    company,
                    url,
                    status,
                    started_at,
                    finished_at,
                    max(int(latency_ms), 0),
                    max(int(fetched_count), 0),
                    max(int(matched_count), 0),
                    max(int(new_count), 0),
                    max(int(yes_count), 0),
                    max(int(maybe_count), 0),
                    max(int(stale_count), 0),
                    float(jd_coverage or 0.0),
                    error_text or "",
                ),
            )

    def list_source_runs(
        self,
        *,
        limit: int = 300,
        entity_type: str = "",
        mode: str = "",
        status: str = "",
    ) -> list[dict]:
        sql = ["SELECT * FROM source_runs"]
        params: list[object] = []
        where: list[str] = []
        if entity_type:
            where.append("entity_type=?")
            params.append(entity_type)
        if mode:
            where.append("mode=?")
            params.append(mode)
        if status:
            where.append("status=?")
            params.append(status)
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY finished_at DESC, id DESC LIMIT ?")
        params.append(max(limit, 1))
        rows = self._conn.execute(" ".join(sql), tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_source_health(
        self,
        *,
        entity_type: str = "",
        mode: str = "",
        status: str = "",
        limit: int = 500,
    ) -> list[dict]:
        runs = self.list_source_runs(limit=5000, entity_type=entity_type, mode=mode)
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in runs:
            key = (row["source_key"], row["entity_type"])
            grouped.setdefault(key, []).append(row)

        items: list[dict] = []
        for (_source_key, _entity_type), rows in grouped.items():
            latest = rows[0]
            basis = latest
            if latest["status"] == "skipped" and str(latest["error_text"] or "").startswith("Skipped because board was checked within"):
                basis = next((row for row in rows[1:] if row["status"] != "skipped"), latest)
            recent = rows[:10]
            successes = [row for row in recent if row["status"] == "success"]
            last_success = next((row["finished_at"] for row in rows if row["status"] == "success"), "")
            last_error = next((row["error_text"] for row in rows if row["status"] != "success" and row["error_text"]), "")
            failure_streak = 0
            latest_jd_coverage = float(basis["jd_coverage"] or 0.0)
            latest_fetched = int(basis["fetched_count"] or 0)
            for row in rows:
                if row["status"] in {"success", "empty"}:
                    break
                if row["status"] == "skipped" and str(row["error_text"] or "").startswith("Skipped because board was checked within"):
                    continue
                failure_streak += 1
            success_rate = (100.0 * len(successes) / len(recent)) if recent else 0.0
            avg_latency = sum(int(row["latency_ms"] or 0) for row in recent) / len(recent) if recent else 0.0
            avg_fetched = sum(int(row["fetched_count"] or 0) for row in recent) / len(recent) if recent else 0.0
            if basis["status"] == "success":
                health = "healthy"
            elif basis["status"] == "skipped":
                health = "degraded" if not last_success else "healthy"
            elif basis["status"] == "empty":
                health = "degraded"
            else:
                health = "broken"
            if basis["entity_type"] == "main" and basis["status"] == "success" and latest_fetched >= 10 and latest_jd_coverage < 5.0:
                health = "degraded"
            if failure_streak >= 2 and basis["status"] not in {"success", "empty", "skipped"}:
                health = "broken"

            item = {
                "source_key": latest["source_key"],
                "entity_type": latest["entity_type"],
                "mode": latest["mode"],
                "platform": latest["platform"],
                "company": latest["company"],
                "url": latest["url"],
                "latest_status": latest["status"],
                "basis_status": basis["status"],
                "health": health,
                "latest_started_at": latest["started_at"],
                "latest_finished_at": latest["finished_at"],
                "latest_latency_ms": int(latest["latency_ms"] or 0),
                "latest_fetched_count": latest_fetched,
                "latest_matched_count": int(basis["matched_count"] or 0),
                "latest_new_count": int(basis["new_count"] or 0),
                "latest_yes_count": int(basis["yes_count"] or 0),
                "latest_maybe_count": int(basis["maybe_count"] or 0),
                "latest_stale_count": int(basis["stale_count"] or 0),
                "latest_jd_coverage": latest_jd_coverage,
                "failure_streak": failure_streak,
                "last_success_at": last_success,
                "last_error": last_error,
                "recent_runs": len(recent),
                "recent_success_rate": round(success_rate, 1),
                "avg_latency_ms": round(avg_latency, 1),
                "avg_fetched_count": round(avg_fetched, 1),
            }
            if status and item["health"] != status and item["latest_status"] != status:
                continue
            items.append(item)

        items.sort(
            key=lambda item: (
                {"broken": 0, "degraded": 1, "healthy": 2}.get(item["health"], 3),
                item["latest_finished_at"],
            ),
            reverse=False,
        )
        items.sort(key=lambda item: item["latest_finished_at"], reverse=True)
        items.sort(key=lambda item: {"broken": 0, "degraded": 1, "healthy": 2}.get(item["health"], 3))
        return items[: max(limit, 1)]

    def get_health_summary(self) -> dict:
        health_rows = self.get_source_health(limit=5000)
        total = len(health_rows)
        healthy = sum(1 for row in health_rows if row["health"] == "healthy")
        degraded = sum(1 for row in health_rows if row["health"] == "degraded")
        broken = sum(1 for row in health_rows if row["health"] == "broken")
        cutoff = datetime.now(timezone.utc).timestamp() - 86400
        failures_24h = 0
        empty_24h = 0
        skipped_24h = 0
        for row in self.list_source_runs(limit=5000):
            try:
                finished = datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00"))
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
                if finished.timestamp() >= cutoff:
                    if row["status"] == "error":
                        failures_24h += 1
                    elif row["status"] == "empty":
                        empty_24h += 1
                    elif row["status"] == "skipped":
                        skipped_24h += 1
            except Exception:
                continue
        return {
            "total": total,
            "healthy": healthy,
            "degraded": degraded,
            "broken": broken,
            "failures_24h": failures_24h,
            "empty_24h": empty_24h,
            "skipped_24h": skipped_24h,
        }

    # -------------------------------------------------------------------------
    # Cursors (pagination state)
    # -------------------------------------------------------------------------

    def get_cursor(self, name: str) -> int:
        row = self._conn.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        if row is None:
            return 0
        try:
            return max(int(row["value"]), 0)
        except ValueError:
            return 0

    def get_cursor_value(self, name: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        if row is None:
            return default
        value = row["value"]
        return default if value is None else str(value)

    def set_cursor(self, name: str, value: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO cursors(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, str(max(value, 0))),
            )

    def set_cursor_value(self, name: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO cursors(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, "" if value is None else str(value)),
            )

    # -------------------------------------------------------------------------
    # Reporting helpers
    # -------------------------------------------------------------------------

    def expire_old_jobs(self, days: int = 14) -> int:
        """Delete jobs not seen within the last `days` days. Returns count deleted."""
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM jobs WHERE last_seen < datetime('now', ?)",
                (f"-{days} days",),
            )
            deleted = cur.rowcount
        if deleted:
            log.info("Expired %d stale job(s) older than %d days", deleted, days)
        return deleted

    def is_duplicate_title(self, company: str, title: str) -> bool:
        """Return True if we already have a job with the same company+title in the DB."""
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE lower(company)=lower(?) AND lower(title)=lower(?) LIMIT 1",
            (company.strip(), title.strip()),
        ).fetchone()
        return row is not None

    def get_stats(self) -> dict:
        """Return summary statistics used by the weekly health-check email."""
        total = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        new_24h = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE first_seen >= datetime('now','-1 day')"
        ).fetchone()[0]
        new_7d = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE first_seen >= datetime('now','-7 days')"
        ).fetchone()[0]
        yes_count = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE label='yes'"
        ).fetchone()[0]
        maybe_count = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE label='maybe'"
        ).fetchone()[0]
        last_activity = self._conn.execute(
            "SELECT MAX(last_seen) FROM jobs"
        ).fetchone()[0] or "never"
        board_stats = self.get_board_stats()
        return {
            "total_jobs": total,
            "new_24h": new_24h,
            "new_7d": new_7d,
            "yes_count": yes_count,
            "maybe_count": maybe_count,
            "last_activity": last_activity,
            "boards": board_stats,
        }

    def export_dead_boards_csv(self, out_path: str) -> None:
        import csv

        rows = self.get_dead_boards()
        if not rows or not out_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        fieldnames = ["board_id", "platform", "company", "url", "fail_count", "fail_reason", "last_checked"]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        log.info("Exported %d dead boards to %s", len(rows), out_path)

    def get_feature_flags(self, defaults: dict[str, bool]) -> dict[str, bool]:
        rows = self._conn.execute("SELECT name, enabled FROM feature_flags").fetchall()
        flags = {name: bool(enabled) for name, enabled in defaults.items()}
        for row in rows:
            flags[row["name"]] = bool(row["enabled"])
        return flags

    def set_feature_flag(self, name: str, enabled: bool) -> None:
        now = _now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO feature_flags(name, enabled, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (name, 1 if enabled else 0, now),
            )

    def save_generated_resume(
        self,
        *,
        job_key: str,
        job_title: str,
        company: str,
        content: str,
        format: str = "markdown",
    ) -> int:
        now = _now()
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO generated_resumes(job_key, job_title, company, format, content, created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (job_key, job_title, company, format, content, now),
            )
            return int(cur.lastrowid)

    def list_generated_resumes(self, job_key: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT id, job_key, job_title, company, format, content, created_at
            FROM generated_resumes
            WHERE job_key=?
            ORDER BY id DESC
            """,
            (job_key,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_generated_resume(self, resume_id: int) -> Optional[dict]:
        row = self._conn.execute(
            """
            SELECT id, job_key, job_title, company, format, content, created_at
            FROM generated_resumes
            WHERE id=?
            """,
            (resume_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def save_generated_artifact(
        self,
        *,
        resume_id: int,
        name: str,
        content: str,
        format: str = "text",
        filename: str = "",
    ) -> int:
        now = _now()
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO generated_artifacts(resume_id, name, format, filename, content, created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (resume_id, name, format, filename, content, now),
            )
            return int(cur.lastrowid)

    def list_generated_artifacts(self, resume_id: int) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT id, resume_id, name, format, filename, content, created_at
            FROM generated_artifacts
            WHERE resume_id=?
            ORDER BY id ASC
            """,
            (resume_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_generated_artifact(self, artifact_id: int) -> Optional[dict]:
        row = self._conn.execute(
            """
            SELECT id, resume_id, name, format, filename, content, created_at
            FROM generated_artifacts
            WHERE id=?
            """,
            (artifact_id,),
        ).fetchone()
        return dict(row) if row is not None else None
