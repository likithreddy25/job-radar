"""Lightweight web UI for browsing jobs, toggling features, and generating resumes."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote
from urllib.error import URLError
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server

from .config import Config
from .database import Database
from .evaluation import EVAL_CFG, EVALUATION_PAYLOAD_VERSION, EvaluationDimension, EvaluationResult, evaluate_job
from .job_intelligence import extract_workday_req_id
from .llm_scorer import apply_llm_delta, review_jobs_batch
from .scoring_policy import calibrate_thresholds, label_for_score
from .resume_builder import generate_resume_packet
from .sources.base import is_us_location, remote_scope_status

PIPELINE_STATUSES = [
    "new",
    "shortlisted",
    "resume_generated",
    "applied",
    "interview",
    "onsite",
    "offer",
    "screen_reject",
    "ghosted",
    "rejected",
    "archived",
]
GRADE_SCORES = {
    "A": 6,
    "B": 5,
    "C": 4,
    "D": 3,
    "E": 2,
    "F": 1,
}

RECENT_JOB_DAYS = 1
DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d",
)

log = logging.getLogger(__name__)
STATE_DB_FILENAMES = ("gha-jobs.db", "gha-boards.db")
GITHUB_ARCHIVE_DB_FILENAME = "github-archive.db"
BROAD_NON_WORKDAY_BOARDS_CSV = "data/boards/BROAD_NON_WORKDAY_EXTRA.csv"
BROAD_NON_WORKDAY_CURSOR_KEY = "boards_broad_non_workday"
BROAD_NON_WORKDAY_BATCH_SIZE = 300
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
GITHUB_SYNC_SOURCES = (
    {
        "name": "Your GitHub jobs snapshot",
        "slug": "origin",
        "provenance": "github-mine",
        "url": "https://raw.githubusercontent.com/chandalagufus/job/main/state/gha-jobs.db",
    },
    {
        "name": "Rohith GitHub jobs snapshot",
        "slug": "rohith",
        "provenance": "github-rohith",
        "url": "https://raw.githubusercontent.com/Rohith066/job-radar/main/state/jobs.db",
    },
)


def _resolve_db_path(repo_root: str, raw_path: str) -> Path:
    candidate = Path(os.path.expanduser(raw_path))
    if candidate.is_absolute():
        return candidate
    base = Path(repo_root) if repo_root else Path.cwd()
    return (base / candidate).resolve()


def _state_db_paths(root: Path) -> list[Path]:
    return [(root / "state" / name).resolve() for name in STATE_DB_FILENAMES]


def _github_archive_db_path(root: Path) -> Path:
    return (root / "state" / GITHUB_ARCHIVE_DB_FILENAME).resolve()


def _db_snapshot(path: Path) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "path": path,
        "exists": path.exists(),
        "healthy": False,
        "total_jobs": 0,
        "recent_jobs_24h": 0,
        "recent_first_seen_24h": 0,
        "last_seen": "",
        "fresh_ts": 0.0,
    }
    if not path.exists():
        return snapshot
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT source, posted, first_seen, last_seen FROM jobs"
            ).fetchall()
        ]
        snapshot["total_jobs"] = len(rows)
        snapshot["recent_jobs_24h"] = sum(
            1 for row in rows if _passes_freshness(row, days_raw="1", max_days=1)
        )
        snapshot["recent_first_seen_24h"] = sum(
            1 for row in rows if _passes_freshness(row, days_raw="seen_1", max_days=1)
        )
        last_seen = max((str(row.get("last_seen") or "") for row in rows), default="")
        snapshot["last_seen"] = last_seen
        parsed = _parse_datetime(last_seen)
        snapshot["fresh_ts"] = parsed.timestamp() if parsed else 0.0
        snapshot["healthy"] = True
        return snapshot
    except sqlite3.Error:
        return snapshot
    finally:
        conn.close()


def _dashboard_source_label(repo_root: str, path: Path) -> str:
    root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    resolved = path.resolve()
    public_export = (root / "public-export").resolve()
    if resolved == _github_archive_db_path(root):
        return "github"
    if resolved == _github_archive_db_path(public_export):
        return "github"
    if resolved in set(_state_db_paths(public_export)):
        return "public-export"
    if root.name.lower() == "public-export" and resolved in set(_state_db_paths(root)):
        return "public-export"
    if resolved in set(_state_db_paths(root)):
        return "local"
    if "dashboard-merged-" in resolved.name:
        return "merged"
    return resolved.parent.name or "local"


def _copy_db_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    for sidecar in (
        dst,
        dst.with_name(dst.name + "-wal"),
        dst.with_name(dst.name + "-shm"),
    ):
        try:
            if sidecar.exists():
                sidecar.unlink()
        except OSError:
            pass
    source = sqlite3.connect(str(src), timeout=30)
    target = sqlite3.connect(str(dst), timeout=30)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        target.execute("PRAGMA busy_timeout=30000")
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()


def _download_remote_db(url: str, dst: Path, *, timeout: int = 180) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "job-radar-dashboard/1.0"})
    with urlopen(req, timeout=timeout) as resp, open(dst, "wb") as out:
        shutil.copyfileobj(resp, out)


def _import_missing_jobs_from_db(
    local_db_path: Path,
    overlay_db_path: Path,
    *,
    prefer_overlay_scores: bool = False,
    provenance_label: str = "",
) -> dict[str, int]:
    conn = sqlite3.connect(str(local_db_path), timeout=60)
    overlay = sqlite3.connect(str(overlay_db_path), timeout=60)
    try:
        conn.row_factory = sqlite3.Row
        overlay.row_factory = sqlite3.Row
        before = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        local_cols = [str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        overlay_cols = [str(row["name"]) for row in overlay.execute("PRAGMA table_info(jobs)").fetchall()]
        overlay_col_set = set(overlay_cols)
        common_cols = [col for col in local_cols if col in overlay_col_set]
        write_cols = list(common_cols)
        if "provenance" in local_cols and "provenance" not in write_cols:
            write_cols.append("provenance")
        if "key" not in common_cols:
            return {"overlay_total": 0, "overlap": 0, "inserted": 0, "updated": 0}
        column_sql = ", ".join(common_cols)
        select_sql = f"SELECT {column_sql} FROM jobs"
        existing_sql = f"SELECT {', '.join(write_cols)} FROM jobs"
        placeholders = ", ".join("?" for _ in write_cols)
        upsert_sql = f"INSERT OR REPLACE INTO jobs ({', '.join(write_cols)}) VALUES ({placeholders})"
        overlay_rows = overlay.execute(select_sql).fetchall()
        overlay_total = len(overlay_rows)
        overlap = 0
        updated = 0
        for row in overlay_rows:
            row_values = {col: row[col] for col in common_cols}
            if "provenance" in write_cols:
                row_values["provenance"] = provenance_label or str(row_values.get("provenance") or "local")
            existing = conn.execute(existing_sql + " WHERE key=?", (row["key"],)).fetchone()
            if existing is None:
                conn.execute(upsert_sql, tuple(row_values.get(col, "") for col in write_cols))
                continue
            overlap += 1
            merged_values = _merge_archive_job_rows(
                existing,
                row_values,
                write_cols,
                prefer_overlay_scores=prefer_overlay_scores,
            )
            if merged_values != tuple(existing[col] for col in write_cols):
                conn.execute(upsert_sql, merged_values)
                updated += 1
        conn.commit()
        after = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        return {
            "overlay_total": overlay_total,
            "overlap": overlap,
            "inserted": max(after - before, 0),
            "updated": updated,
        }
    finally:
        overlay.close()
        conn.close()


def _prefer_non_empty(primary: object, fallback: object) -> object:
    primary_text = str(primary or "").strip()
    if primary_text:
        return primary
    return fallback


def _merge_provenance_text(primary: object, fallback: object) -> str:
    tokens: list[str] = []
    for raw in (str(primary or ""), str(fallback or "")):
        for piece in raw.split("+"):
            item = piece.strip()
            if item and item not in tokens:
                tokens.append(item)
    if not tokens:
        return "local"
    return "+".join(tokens)


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


def _prefer_longer_text(primary: object, fallback: object) -> object:
    primary_text = str(primary or "").strip()
    fallback_text = str(fallback or "").strip()
    if len(fallback_text) > len(primary_text):
        return fallback
    return primary


def _prefer_newer_timestamp_text(primary: object, fallback: object) -> object:
    primary_text = str(primary or "").strip()
    fallback_text = str(fallback or "").strip()
    primary_dt = _parse_datetime(primary_text)
    fallback_dt = _parse_datetime(fallback_text)
    if primary_dt and fallback_dt:
        return fallback if fallback_dt > primary_dt else primary
    if fallback_dt and not primary_dt:
        return fallback
    if primary_dt and not fallback_dt:
        return primary
    return fallback if fallback_text > primary_text else primary


def _prefer_earlier_timestamp_text(primary: object, fallback: object) -> object:
    primary_text = str(primary or "").strip()
    fallback_text = str(fallback or "").strip()
    primary_dt = _parse_datetime(primary_text)
    fallback_dt = _parse_datetime(fallback_text)
    if primary_dt and fallback_dt:
        return fallback if fallback_dt < primary_dt else primary
    if fallback_dt and not primary_dt:
        return fallback
    if primary_dt and not fallback_dt:
        return primary
    if not primary_text:
        return fallback
    if not fallback_text:
        return primary
    return fallback if fallback_text < primary_text else primary


def _merge_job_rows(existing_row: sqlite3.Row, incoming_row: sqlite3.Row, columns: list[str]) -> tuple[object, ...]:
    merged = {column: existing_row[column] for column in columns}
    incoming = {column: incoming_row[column] for column in columns}

    for field in ("title", "location", "url", "canonical_key", "repost_of_key", "employer_quality_reason", "viewed_at"):
        merged[field] = _prefer_non_empty(merged.get(field), incoming.get(field))
    if "provenance" in merged:
        merged["provenance"] = _merge_provenance_text(merged.get("provenance"), incoming.get("provenance"))

    existing_eval = str(merged.get("evaluation_json") or "").strip()
    existing_fit = str(merged.get("fit_summary") or "").strip()
    incoming_eval = str(incoming.get("evaluation_json") or "").strip()
    incoming_fit = str(incoming.get("fit_summary") or "").strip()
    incoming_score = int(incoming.get("score") or 0)
    incoming_label = str(incoming.get("label") or "").strip()

    for field in ("description", "structured_json", "fit_summary", "evaluation_json"):
        merged[field] = _prefer_longer_text(merged.get(field), incoming.get(field))

    merged["posted"] = _prefer_newer_timestamp_text(merged.get("posted"), incoming.get("posted"))
    merged["last_seen"] = _prefer_newer_timestamp_text(merged.get("last_seen"), incoming.get("last_seen"))
    merged["first_seen"] = _prefer_earlier_timestamp_text(merged.get("first_seen"), incoming.get("first_seen"))

    merged["is_repost"] = max(int(merged.get("is_repost") or 0), int(incoming.get("is_repost") or 0))
    merged["manual_input"] = max(int(merged.get("manual_input") or 0), int(incoming.get("manual_input") or 0))
    merged["employer_quality_score"] = max(
        int(merged.get("employer_quality_score") or 0),
        int(incoming.get("employer_quality_score") or 0),
    )

    chose_incoming_eval = (
        incoming_eval
        and incoming_eval != existing_eval
        and str(merged.get("evaluation_json") or "").strip() == incoming_eval
    )
    chose_incoming_fit = (
        incoming_fit
        and incoming_fit != existing_fit
        and str(merged.get("fit_summary") or "").strip() == incoming_fit
    )
    if chose_incoming_eval or chose_incoming_fit or (not existing_eval and (incoming_eval or incoming_fit or incoming_label or incoming_score > 0)):
        if incoming_score > 0 or not int(merged.get("score") or 0):
            merged["score"] = incoming_score
        if incoming_label:
            merged["label"] = incoming_label

    merged["grade"] = _score_to_grade(int(merged.get("score") or 0))

    return tuple(merged[column] for column in columns)


def _merge_archive_job_rows(
    existing_row: sqlite3.Row,
    incoming_row: sqlite3.Row,
    columns: list[str],
    *,
    prefer_overlay_scores: bool = False,
) -> tuple[object, ...]:
    merged = {column: existing_row[column] for column in columns}
    incoming = {column: incoming_row[column] for column in columns}

    for field in ("title", "location", "url", "canonical_key", "repost_of_key", "employer_quality_reason", "viewed_at"):
        merged[field] = _prefer_non_empty(merged.get(field), incoming.get(field))
    if "provenance" in merged:
        merged["provenance"] = _merge_provenance_text(merged.get("provenance"), incoming.get("provenance"))

    existing_desc = str(merged.get("description") or "").strip()
    incoming_desc = str(incoming.get("description") or "").strip()
    existing_eval = str(merged.get("evaluation_json") or "").strip()
    incoming_eval = str(incoming.get("evaluation_json") or "").strip()
    incoming_fit = str(incoming.get("fit_summary") or "").strip()
    incoming_score = int(incoming.get("score") or 0)
    incoming_label = str(incoming.get("label") or "").strip()
    incoming_grade = str(incoming.get("grade") or "").strip()

    for field in ("description", "structured_json", "fit_summary", "evaluation_json"):
        merged[field] = _prefer_longer_text(merged.get(field), incoming.get(field))

    merged["posted"] = _prefer_newer_timestamp_text(merged.get("posted"), incoming.get("posted"))
    merged["last_seen"] = _prefer_newer_timestamp_text(merged.get("last_seen"), incoming.get("last_seen"))
    merged["first_seen"] = _prefer_earlier_timestamp_text(merged.get("first_seen"), incoming.get("first_seen"))

    merged["is_repost"] = max(int(merged.get("is_repost") or 0), int(incoming.get("is_repost") or 0))
    merged["manual_input"] = max(int(merged.get("manual_input") or 0), int(incoming.get("manual_input") or 0))
    merged["employer_quality_score"] = max(
        int(merged.get("employer_quality_score") or 0),
        int(incoming.get("employer_quality_score") or 0),
    )

    should_overlay_scores = False
    if prefer_overlay_scores and (incoming_eval or incoming_label or incoming_score > 0):
        existing_score = int(merged.get("score") or 0)
        if not existing_eval:
            should_overlay_scores = True
        elif incoming_score > 0 and existing_score == 0:
            should_overlay_scores = True
        elif len(incoming_desc) > len(existing_desc):
            should_overlay_scores = True

    if should_overlay_scores:
        if incoming_score > 0 or not int(merged.get("score") or 0):
            merged["score"] = incoming_score
        if incoming_label:
            merged["label"] = incoming_label
        if incoming_eval:
            merged["evaluation_json"] = incoming_eval
        if incoming_fit:
            merged["fit_summary"] = incoming_fit
    merged["grade"] = _score_to_grade(int(merged.get("score") or 0))

    return tuple(merged[column] for column in columns)


def _merge_jobs_into(base_path: Path, overlay_path: Path, *, prefer_base_on_conflict: bool = False) -> None:
    base = sqlite3.connect(str(base_path))
    overlay = sqlite3.connect(str(overlay_path))
    try:
        base.row_factory = sqlite3.Row
        overlay.row_factory = sqlite3.Row
        columns = [str(row["name"]) for row in overlay.execute("PRAGMA table_info(jobs)").fetchall()]
        if not columns:
            return
        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        select_sql = f"SELECT {column_sql} FROM jobs"
        upsert_sql = f"INSERT OR REPLACE INTO jobs ({column_sql}) VALUES ({placeholders})"
        for row in overlay.execute(select_sql).fetchall():
            existing = base.execute(select_sql + " WHERE key=?", (row["key"],)).fetchone()
            if existing is None:
                base.execute(upsert_sql, tuple(row[col] for col in columns))
                continue
            if prefer_base_on_conflict:
                continue
            merged_values = _merge_job_rows(existing, row, columns)
            base.execute(upsert_sql, merged_values)
        base.commit()
    finally:
        overlay.close()
        base.close()


def _table_columns_for(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _merge_boards_into(base_path: Path, overlay_path: Path) -> None:
    base = sqlite3.connect(str(base_path))
    overlay = sqlite3.connect(str(overlay_path))
    try:
        base.row_factory = sqlite3.Row
        overlay.row_factory = sqlite3.Row
        columns = [col for col in _table_columns_for(base, "boards") if col in set(_table_columns_for(overlay, "boards"))]
        if not columns or "board_id" not in columns:
            return
        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        upsert_sql = f"INSERT OR REPLACE INTO boards ({column_sql}) VALUES ({placeholders})"
        for row in overlay.execute(f"SELECT {column_sql} FROM boards").fetchall():
            existing = base.execute("SELECT last_checked FROM boards WHERE board_id=?", (row["board_id"],)).fetchone()
            if existing is not None:
                existing_checked = str(existing["last_checked"] or "")
                incoming_checked = str(row["last_checked"] or "")
                if existing_checked and incoming_checked and existing_checked >= incoming_checked:
                    continue
            base.execute(upsert_sql, tuple(row[col] for col in columns))
        base.commit()
    finally:
        overlay.close()
        base.close()


def _merge_source_runs_into(base_path: Path, overlay_path: Path) -> None:
    base = sqlite3.connect(str(base_path))
    try:
        base.row_factory = sqlite3.Row
        overlay = sqlite3.connect(str(overlay_path))
        overlay.row_factory = sqlite3.Row
        try:
            overlay_columns = set(_table_columns_for(overlay, "source_runs"))
        finally:
            overlay.close()
        columns = [col for col in _table_columns_for(base, "source_runs") if col != "id" and col in overlay_columns]
        required = {"source_key", "entity_type", "mode", "started_at", "finished_at", "status"}
        if not columns or not required.issubset(set(columns)):
            return
        column_sql = ", ".join(columns)
        source_column_sql = ", ".join(f"overlay_runs.{col}" for col in columns)
        overlay_alias = "overlay_health"
        base.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_source_runs_dashboard_merge
            ON source_runs(source_key, entity_type, mode, started_at, finished_at, status)
            """
        )
        base.execute(f"ATTACH DATABASE ? AS {overlay_alias}", (str(overlay_path),))
        try:
            base.execute(
                f"""
                INSERT INTO source_runs ({column_sql})
                SELECT {source_column_sql}
                FROM {overlay_alias}.source_runs AS overlay_runs
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM source_runs AS existing_runs
                    WHERE existing_runs.source_key = overlay_runs.source_key
                      AND existing_runs.entity_type = overlay_runs.entity_type
                      AND existing_runs.mode = overlay_runs.mode
                      AND existing_runs.started_at = overlay_runs.started_at
                      AND existing_runs.finished_at = overlay_runs.finished_at
                      AND existing_runs.status = overlay_runs.status
                )
                """
            )
            base.commit()
        finally:
            base.execute(f"DETACH DATABASE {overlay_alias}")
    finally:
        base.close()


def _safe_merge_dashboard_db(
    repo_root: str,
    snaps: list[dict[str, object]],
    *,
    strategy_label: str,
) -> tuple[Path, str]:
    if strategy_label == "merged-public+local":
        ordered = sorted(
            snaps,
            key=lambda snap: (
                1 if _dashboard_source_label(repo_root, Path(snap["path"])) == "local" else 0,
                float(snap["fresh_ts"]),
                int(snap["recent_jobs_24h"]),
                int(snap["total_jobs"]),
            ),
            reverse=True,
        )
    else:
        ordered = sorted(
            snaps,
            key=lambda snap: (
                float(snap["fresh_ts"]),
                int(snap["recent_jobs_24h"]),
                int(snap["total_jobs"]),
            ),
            reverse=True,
        )
    base_snap = ordered[0]
    overlays = ordered[1:]
    merge_key = "|".join(
        f"{Path(snap['path']).name}:{int(float(snap['fresh_ts']))}:{int(snap['total_jobs'])}" for snap in ordered
    )
    merge_hash = hashlib.sha1(merge_key.encode("utf-8")).hexdigest()[:12]
    merged_name = f"dashboard-merged-{merge_hash}.db"
    merged_path = (Path(repo_root) / "state" / merged_name).resolve() if repo_root else Path(base_snap["path"])
    try:
        with tempfile.TemporaryDirectory(prefix="job-radar-dashboard-merge-") as temp_dir:
            temp_root = Path(temp_dir)
            snapshot_paths: list[Path] = []
            for index, snap in enumerate(ordered):
                snapshot_path = temp_root / f"input-{index}.db"
                _copy_db_file(Path(snap["path"]), snapshot_path)
                snapshot_paths.append(snapshot_path)

            _copy_db_file(snapshot_paths[0], merged_path)
            prefer_base_on_conflict = strategy_label == "merged-public+local"
            for overlay_path in snapshot_paths[1:]:
                _merge_jobs_into(
                    merged_path,
                    overlay_path,
                    prefer_base_on_conflict=prefer_base_on_conflict,
                )
                _merge_boards_into(merged_path, overlay_path)
                _merge_source_runs_into(merged_path, overlay_path)
        return merged_path, strategy_label
    except (sqlite3.Error, OSError) as exc:
        try:
            _copy_db_file(Path(base_snap["path"]), merged_path)
            prefer_base_on_conflict = strategy_label == "merged-public+local"
            for overlay_snap in overlays:
                overlay_path = Path(overlay_snap["path"])
                if not overlay_path.exists():
                    continue
                overlay_snapshot = merged_path.with_name(f"{merged_path.stem}-overlay-{hashlib.sha1(str(overlay_path).encode('utf-8')).hexdigest()[:8]}.db")
                try:
                    _copy_db_file(overlay_path, overlay_snapshot)
                    _merge_jobs_into(
                        merged_path,
                        overlay_snapshot,
                        prefer_base_on_conflict=prefer_base_on_conflict,
                    )
                    _merge_boards_into(merged_path, overlay_snapshot)
                    _merge_source_runs_into(merged_path, overlay_snapshot)
                finally:
                    try:
                        overlay_snapshot.unlink()
                    except OSError:
                        pass
            return merged_path, strategy_label
        except (sqlite3.Error, OSError):
            pass
        base_path = Path(base_snap["path"])
        log.warning(
            "Dashboard DB merge failed for %s: %s. Falling back to %s.",
            base_path,
            exc,
            base_path,
        )
        return base_path, f"{strategy_label}-fallback"


def _select_dashboard_db(repo_root: str, db_path: str, dataset_preference: str = "auto") -> tuple[Path, str]:
    primary = _resolve_db_path(repo_root, db_path)
    root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    candidate_paths: list[Path] = [primary]
    candidate_paths.extend(path for path in _state_db_paths(root) if path != primary)
    candidate_paths.append(_github_archive_db_path(root))
    nested_public_export = (root / "public-export").resolve()
    candidate_paths.extend(path for path in _state_db_paths(nested_public_export) if path != primary)
    candidate_paths.append(_github_archive_db_path(nested_public_export))
    if root.name.lower() == "public-export":
        candidate_paths.extend(path for path in _state_db_paths(root.parent.resolve()) if path != primary)
        candidate_paths.append(_github_archive_db_path(root.parent.resolve()))
    deduped_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for path in candidate_paths:
        if path in seen_paths:
            continue
        deduped_paths.append(path)
        seen_paths.add(path)

    existing = [snap for snap in (_db_snapshot(path) for path in deduped_paths) if snap["exists"] and bool(snap.get("healthy"))]
    if not existing:
        return primary, "primary-missing"

    grouped: dict[str, list[dict[str, object]]] = {}
    for snap in existing:
        label = _dashboard_source_label(repo_root, Path(snap["path"]))
        grouped.setdefault(label, []).append(snap)

    def _collapse_group(label: str) -> dict[str, object] | None:
        snaps = grouped.get(label, [])
        if not snaps:
            return None
        if len(snaps) == 1:
            return snaps[0]
        merged_path, _ = _safe_merge_dashboard_db(repo_root, snaps, strategy_label=f"{label}-internal")
        merged_snap = _db_snapshot(merged_path)
        if merged_snap["exists"] and bool(merged_snap.get("healthy")):
            return merged_snap
        return snaps[0]

    public_candidate = _collapse_group("public-export")
    local_candidate = _collapse_group("local")
    github_candidate = _collapse_group("github")
    requested = (dataset_preference or "auto").strip().lower()

    if requested == "merged":
        if public_candidate and local_candidate:
            return _safe_merge_dashboard_db(
                repo_root,
                [public_candidate, local_candidate],
                strategy_label="merged-public+local",
            )
        if local_candidate:
            return Path(local_candidate["path"]), "merged-request-fallback-local"
        if public_candidate:
            return Path(public_candidate["path"]), "merged-request-fallback-public"

    if requested == "merged-all":
        merge_inputs = [snap for snap in (public_candidate, local_candidate, github_candidate) if snap]
        if len(merge_inputs) >= 2:
            return _safe_merge_dashboard_db(
                repo_root,
                merge_inputs,
                strategy_label="merged-public+local+github",
            )
        if local_candidate:
            return Path(local_candidate["path"]), "merged-all-fallback-local"
        if github_candidate:
            return Path(github_candidate["path"]), "merged-all-fallback-github"
        if public_candidate:
            return Path(public_candidate["path"]), "merged-all-fallback-public"

    if requested == "local":
        if local_candidate:
            return Path(local_candidate["path"]), "local-selected"
        if public_candidate:
            return Path(public_candidate["path"]), "local-selected-fallback-public"

    if requested == "public":
        if public_candidate:
            return Path(public_candidate["path"]), "public-selected"
        if local_candidate:
            return Path(local_candidate["path"]), "public-selected-fallback-local"

    if requested in {"github", "github-mine", "github-rohith"}:
        if github_candidate:
            return Path(github_candidate["path"]), f"{requested}-selected"
        if local_candidate:
            return Path(local_candidate["path"]), f"{requested}-selected-fallback-local"
        if public_candidate:
            return Path(public_candidate["path"]), f"{requested}-selected-fallback-public"

    if public_candidate and local_candidate:
        if int(public_candidate["recent_jobs_24h"]) > 0 and int(local_candidate["recent_jobs_24h"]) > 0:
            return _safe_merge_dashboard_db(
                repo_root,
                [public_candidate, local_candidate],
                strategy_label="merged-public+local",
            )
        return Path(public_candidate["path"]), "public-truth"

    if public_candidate:
        return Path(public_candidate["path"]), "public-truth"

    existing.sort(
        key=lambda snap: (
            float(snap["fresh_ts"]),
            int(snap["recent_jobs_24h"]),
            int(snap["total_jobs"]),
        ),
        reverse=True,
    )
    best = existing[0]
    if len(existing) == 1:
        return Path(best["path"]), "single"
    other = existing[1]
    if int(best["recent_jobs_24h"]) > 0 and int(other["recent_jobs_24h"]) > 0:
        return _safe_merge_dashboard_db(
            repo_root,
            [best, other],
            strategy_label="merged",
        )
    return Path(best["path"]), "freshest"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    return cleaned.strip("-") or "job"


def _parse_datetime(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _is_recent_job(job: dict, *, max_days: int = RECENT_JOB_DAYS) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    posted_dt = _parse_datetime(job.get("posted", ""))
    first_seen_dt = _parse_datetime(job.get("first_seen", ""))
    if posted_dt is not None and posted_dt >= cutoff:
        return True
    if first_seen_dt is not None and first_seen_dt >= cutoff:
        return True
    if posted_dt is not None or first_seen_dt is not None:
        return False
    return True


def _is_recent_first_seen(job: dict, *, max_days: int = RECENT_JOB_DAYS) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    first_seen_dt = _parse_datetime(job.get("first_seen", ""))
    if first_seen_dt is not None:
        return first_seen_dt >= cutoff
    return True


def _is_recent_posted_only(job: dict, *, max_days: int = RECENT_JOB_DAYS) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    posted_dt = _parse_datetime(job.get("posted", ""))
    if posted_dt is not None:
        return posted_dt >= cutoff
    return False


def _job_sort_dt(job: dict) -> datetime:
    posted_dt = _parse_datetime(job.get("posted", ""))
    if posted_dt is not None:
        return posted_dt
    first_seen_dt = _parse_datetime(job.get("first_seen", ""))
    if first_seen_dt is not None:
        return first_seen_dt
    return datetime.min.replace(tzinfo=timezone.utc)


def _job_fetch_dt(job: dict) -> datetime:
    first_seen_dt = _parse_datetime(job.get("first_seen", ""))
    if first_seen_dt is not None:
        return first_seen_dt
    return _job_sort_dt(job)


def _job_fetch_hour_dt(job: dict) -> datetime:
    dt = _job_fetch_dt(job)
    if dt.year <= 1970:
        return dt
    local_dt = dt.astimezone(LOCAL_TIMEZONE)
    return local_dt.replace(minute=0, second=0, microsecond=0)


def _job_viewed_sort_value(job: dict) -> int:
    return 1 if str(job.get("viewed_at") or "").strip() else 0


def _format_local_dt(raw_value: object) -> str:
    dt = _parse_datetime(str(raw_value or ""))
    if dt is None:
        return ""
    return dt.astimezone(LOCAL_TIMEZONE).strftime("%b %d, %I:%M %p").replace(" 0", " ")


def _fetched_bucket_label(job: dict) -> str:
    dt = _job_fetch_hour_dt(job)
    if dt.year <= 1970:
        return "Fetched time unknown"
    return f"Fetched {dt.strftime('%b %d, %I %p').replace(' 0', ' ')}"


def _job_sort_ts(job: dict) -> float:
    dt = _job_sort_dt(job)
    if dt.year <= 1970:
        return 0.0
    return dt.timestamp()


def _passes_freshness(job: dict, *, days_raw: str, max_days: int) -> bool:
    if days_raw == "all":
        return True
    if days_raw.startswith("posted_"):
        return _is_recent_posted_only(job, max_days=max_days)
    if days_raw.startswith("seen_"):
        return _is_recent_first_seen(job, max_days=max_days)
    if (job.get("source") or "").strip().lower() == "linkedin":
        return _is_recent_job(job, max_days=1)
    return _is_recent_job(job, max_days=max_days)


def _days_value_to_max_days(days_raw: str) -> int:
    normalized = (days_raw or str(RECENT_JOB_DAYS)).strip().lower()
    if normalized == "all":
        return RECENT_JOB_DAYS
    if normalized.startswith("posted_"):
        normalized = normalized.split("_", 1)[1]
    if normalized.startswith("seen_"):
        normalized = normalized.split("_", 1)[1]
    try:
        return max(int(normalized), 1)
    except ValueError:
        return RECENT_JOB_DAYS


def _source_match(job: dict, source: str) -> bool:
    if not source or source == "all":
        return True
    return (job.get("source") or "").strip().lower() == source


def _score_match(job: dict, min_score: int) -> bool:
    try:
        score = int(job.get("score") or 0)
    except Exception:
        score = 0
    return score >= max(min_score, 0)


def _job_has_github_provenance(job: dict) -> bool:
    provenance = str(job.get("provenance") or "").strip().lower()
    return "github-mine" in provenance or "github-rohith" in provenance


def _provenance_tokens(job: dict) -> list[str]:
    return [part.strip() for part in str(job.get("provenance") or "").strip().lower().split("+") if part.strip()]


def _collected_provenance_token(job: dict) -> str:
    tokens = _provenance_tokens(job)
    if not tokens:
        return ""
    return tokens[0]


def _dataset_match(job: dict, dataset: str) -> bool:
    normalized = (dataset or "merged").strip().lower()
    if normalized == "github":
        return _job_has_github_provenance(job)
    if normalized == "github-mine":
        return _collected_provenance_token(job) == "github-mine"
    if normalized == "github-rohith":
        return _collected_provenance_token(job) == "github-rohith"
    if normalized == "merged-all":
        return True
    if normalized in {"local", "merged"}:
        return not _job_has_github_provenance(job)
    return True


def _sort_jobs(jobs: list[dict], sort_by: str) -> None:
    if sort_by == "oldest":
        jobs.sort(key=lambda job: (_job_sort_dt(job), job["score"]))
        return
    if sort_by == "posted":
        jobs.sort(key=lambda job: (_job_sort_dt(job), int(job.get("score") or 0)), reverse=True)
        return
    if sort_by == "score":
        jobs.sort(key=lambda job: (int(job.get("score") or 0), _job_sort_dt(job)), reverse=True)
        return
    if sort_by == "rating":
        jobs.sort(
            key=lambda job: (
                GRADE_SCORES.get((job.get("grade") or "F").upper(), 0),
                int(job.get("score") or 0),
                _job_sort_dt(job),
            ),
            reverse=True,
        )
        return
    if sort_by == "source":
        jobs.sort(
            key=lambda job: (
                (job.get("source") or "").strip().lower(),
                -int(job.get("score") or 0),
                -_job_sort_ts(job),
            )
        )
        return
    jobs.sort(
        key=lambda job: (
            _job_fetch_hour_dt(job),
            -_job_viewed_sort_value(job),
            _job_fetch_dt(job),
            int(job.get("score") or 0),
            _job_sort_dt(job),
        ),
        reverse=True,
    )


def _queue_match(job: dict, queue: str, status: str) -> bool:
    pipeline_status = (job.get("pipeline_status") or "new").strip().lower()
    if status and status != "all" and pipeline_status != status:
        return False
    if queue == "all":
        return True
    if queue == "active":
        return pipeline_status not in {"archived", "rejected", "screen_reject", "ghosted"}
    if queue == "actionable":
        return pipeline_status in {"new", "shortlisted", "resume_generated", "applied", "interview", "onsite"}
    return True


def _location_allowed_for_review(location: str, *, require_us_location: bool) -> bool:
    if not require_us_location:
        return True
    if is_us_location(location):
        return True
    return remote_scope_status(location) == "unspecified"


def _recent_alert_summary_rows(jobs: list[dict], *, days: int = 3) -> list[dict[str, object]]:
    per_day: dict[str, dict[str, int]] = {}
    for job in jobs:
        first_seen = _parse_datetime(job.get("first_seen", ""))
        if first_seen is None:
            continue
        day_key = first_seen.astimezone(LOCAL_TIMEZONE).date().isoformat()
        bucket = per_day.setdefault(day_key, {"total": 0, "yes": 0, "maybe": 0})
        bucket["total"] += 1
        label = (job.get("label") or "").strip().lower()
        if label == "yes":
            bucket["yes"] += 1
        elif label == "maybe":
            bucket["maybe"] += 1

    ordered_days = sorted(per_day.keys(), reverse=True)[: max(days, 1)]
    rows: list[dict[str, object]] = []
    for day_key in ordered_days:
        bucket = per_day[day_key]
        rows.append(
            {
                "day": day_key,
                "total": bucket["total"],
                "yes": bucket["yes"],
                "maybe": bucket["maybe"],
                "review": bucket["yes"] + bucket["maybe"],
            }
        )
    return rows


def _feature_defaults(cfg: Config) -> dict[str, bool]:
    return {
        "scanner_main": cfg.features.scanner_main,
        "scanner_boards": cfg.features.scanner_boards,
        "notifications": cfg.features.notifications,
        "manual_jd": cfg.features.manual_jd,
        "resume_generation": cfg.features.resume_generation,
        "llm_scoring": cfg.llm_scoring.enabled,
    }


def _job_structured(job: dict) -> dict[str, object]:
    raw = (job.get("structured_json") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _display_provenance(provenance: object) -> str:
    raw = str(provenance or "").strip()
    if not raw:
        return ""
    label_map = {
        "local": "Local",
        "github-mine": "GitHub vasishta02",
        "github-rohith": "GitHub Rohith",
    }
    parts = [part.strip() for part in raw.split("+") if part.strip()]
    display_parts = [label_map.get(part, part) for part in parts]
    return " + ".join(display_parts)


def _provenance_labels(provenance: object) -> tuple[str, str]:
    raw = str(provenance or "").strip().lower()
    if not raw:
        return "", ""
    labels = {
        "local": "Local scan",
        "github-mine": "GitHub vasishta02",
        "github-rohith": "GitHub Rohith",
    }
    tokens = [part.strip() for part in raw.split("+") if part.strip()]
    if not tokens:
        return "", ""
    if "local" in tokens:
        enriched = [labels[token] for token in tokens if token in {"github-mine", "github-rohith"}]
        return labels["local"], " + ".join(enriched)
    collected = labels.get(tokens[0], tokens[0])
    enriched = [labels.get(token, token) for token in tokens[1:]]
    return collected, " + ".join(enriched)


def _evaluation_from_payload(job: dict) -> EvaluationResult | None:
    raw = str(job.get("evaluation_json") or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("version") or 0) != EVALUATION_PAYLOAD_VERSION:
        return None
    dimensions_raw = payload.get("dimensions") or []
    dimensions: list[EvaluationDimension] = []
    if isinstance(dimensions_raw, list):
        for item in dimensions_raw:
            if not isinstance(item, dict):
                continue
            try:
                dimensions.append(
                    EvaluationDimension(
                        name=str(item.get("name") or ""),
                        weight=float(item.get("weight") or 0.0),
                        score=int(item.get("score") or 0),
                        reason=str(item.get("reason") or ""),
                    )
                )
            except Exception:
                continue
    payload_score = int(payload.get("score") or 0)
    payload_label = str(payload.get("label") or "no").strip().lower()
    payload_grade = str(payload.get("grade") or _score_to_grade(payload_score)).strip().upper()
    payload_fit_summary = str(payload.get("fit_summary") or "").strip()

    row_score_raw = job.get("score")
    row_label_raw = job.get("label")
    row_grade_raw = job.get("grade")

    try:
        row_score = int(row_score_raw) if row_score_raw not in (None, "") else None
    except Exception:
        row_score = None
    row_label = str(row_label_raw or "").strip().lower() or None
    row_grade = str(row_grade_raw or "").strip().upper() or None

    # If the stored payload no longer matches the row, treat it as stale and
    # force a fresh evaluation rather than mixing new header fields with old
    # reasons/dimensions.
    if row_score is not None and row_score != payload_score:
        return None
    if row_label is not None and row_label != payload_label:
        return None
    if row_grade is not None and row_grade != payload_grade:
        return None

    score = payload_score
    label = payload_label
    grade = payload_grade
    fit_summary = payload_fit_summary
    if not fit_summary:
        reasons = payload.get("reasons") or []
        if isinstance(reasons, list):
            fit_summary = f"Grade {grade} ({score}/100). " + " ".join(str(reason) for reason in reasons[:3])
    return EvaluationResult(
        score=score,
        label=label,
        grade=grade,
        matched_strong=[str(item) for item in (payload.get("matched_strong") or []) if str(item).strip()],
        matched_moderate=[str(item) for item in (payload.get("matched_moderate") or []) if str(item).strip()],
        unsupported_strong=[str(item) for item in (payload.get("unsupported_strong") or []) if str(item).strip()],
        unsupported_moderate=[str(item) for item in (payload.get("unsupported_moderate") or []) if str(item).strip()],
        critical_skill_gaps=[str(item) for item in (payload.get("critical_skill_gaps") or []) if str(item).strip()],
        reasons=[str(item) for item in (payload.get("reasons") or []) if str(item).strip()],
        fit_summary=fit_summary,
        dimensions=dimensions,
    )


def _display_job_with_fresh_evaluation(job: dict, thresholds, evaluator) -> dict:
    stored = _evaluation_from_payload(job)
    if stored is not None:
        return job
    fresh = evaluator(dict(job), thresholds)
    updated = dict(job)
    updated["score"] = fresh.score
    updated["label"] = fresh.label
    updated["grade"] = fresh.grade
    updated["fit_summary"] = fresh.fit_summary
    updated["evaluation_json"] = fresh.to_json()
    return updated


def _display_job_with_consistent_evaluation(job: dict, thresholds) -> dict:
    """Cheap list-page cleanup; full rescoring stays on detail/batch paths."""
    updated = dict(job)
    raw = str(updated.get("evaluation_json") or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None
        if isinstance(payload, dict) and int(payload.get("version") or 0) == EVALUATION_PAYLOAD_VERSION:
            try:
                score = int(payload.get("score") or updated.get("score") or 0)
            except Exception:
                score = int(updated.get("score") or 0)
            updated["score"] = score
            updated["label"] = str(
                payload.get("label")
                or label_for_score(score, yes_threshold=thresholds.yes, maybe_threshold=thresholds.maybe)
            ).strip().lower()
            updated["grade"] = _score_to_grade(score)
            fit_summary = str(payload.get("fit_summary") or "").strip()
            if fit_summary:
                updated["fit_summary"] = fit_summary
            return updated

    try:
        score = int(updated.get("score") or 0)
    except Exception:
        score = 0
    updated["score"] = score
    updated["grade"] = _score_to_grade(score)
    label = str(updated.get("label") or "").strip().lower()
    if label not in {"yes", "maybe", "no"}:
        updated["label"] = label_for_score(score, yes_threshold=thresholds.yes, maybe_threshold=thresholds.maybe)
    return updated


def _structured_signal_pills(job: dict) -> str:
    data = _job_structured(job)
    pills: list[str] = []
    collected_via, enriched_by = _provenance_labels(job.get("provenance"))
    if collected_via:
        pills.append(f"<span class=\"pill\">Collected: {escape(collected_via)}</span>")
    if enriched_by:
        pills.append(f"<span class=\"pill\">Enriched: {escape(enriched_by)}</span>")
    if not (job.get("description") or "").strip():
        pills.append("<span class=\"pill\">No JD</span>")
    remote_mode = str(data.get("remote_mode") or "").strip()
    if remote_mode:
        pills.append(f"<span class=\"pill\">{escape(remote_mode.title())}</span>")
    salary_min = data.get("salary_min")
    salary_max = data.get("salary_max")
    salary_period = str(data.get("salary_period") or "year")
    if isinstance(salary_min, int) and isinstance(salary_max, int):
        pills.append(f"<span class=\"pill\">${salary_min:,} - ${salary_max:,}/{escape(salary_period)}</span>")
    yoe_min = data.get("years_experience_min")
    yoe_max = data.get("years_experience_max")
    if isinstance(yoe_min, int):
        if isinstance(yoe_max, int):
            pills.append(f"<span class=\"pill\">{yoe_min}-{yoe_max} yrs</span>")
        else:
            pills.append(f"<span class=\"pill\">{yoe_min}+ yrs</span>")
    if data.get("linkedin_verified"):
        pills.append("<span class=\"pill\">Verified</span>")
    if data.get("visa_sponsorship") is True:
        pills.append("<span class=\"pill\">Visa sponsor</span>")
    elif data.get("visa_sponsorship") is False:
        pills.append("<span class=\"pill\">No visa sponsor</span>")
    if data.get("security_clearance"):
        pills.append("<span class=\"pill\">Clearance req</span>")
    if data.get("citizenship_requirement"):
        pills.append("<span class=\"pill\">Citizenship req</span>")
    employment_type = str(data.get("employment_type") or "").strip()
    if employment_type:
        pills.append(f"<span class=\"pill\">{escape(employment_type.title())}</span>")
    return "".join(pills) or "<span class=\"muted\">No extracted signals.</span>"


def _batch_rescore_allowed(job: dict) -> bool:
    provenance = str(job.get("provenance") or "").strip().lower()
    if not provenance:
        return True
    if "github-rohith" in provenance:
        return False
    allowed = {"local", "github-mine"}
    tokens = {part.strip() for part in provenance.split("+") if part.strip()}
    if not tokens:
        return True
    return tokens.issubset(allowed)


def _board_inventory_total(cfg: Config, boards_csv: str = "") -> int:
    try:
        from .main import _resolve_boards_csv
    except Exception:
        return 0
    try:
        path = _resolve_boards_csv(boards_csv or cfg.boards.csv)
    except Exception:
        return 0
    total = 0
    with open(path, encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            company = (row.get("company_name") or row.get("company") or "").strip()
            platform = (row.get("platform") or "").strip().lower()
            url = (row.get("board_url") or row.get("url") or "").strip()
            ok_val = (row.get("ok") or "").strip().lower()
            if ok_val and ok_val not in ("true", "1", "yes"):
                continue
            if company and platform and url:
                total += 1
    return total


def _workday_legacy_duplicate_keys(jobs: list[dict]) -> set[str]:
    by_req: dict[str, dict[str, set[str]]] = {}
    for job in jobs:
        if (job.get("source") or "").strip().lower() != "workday":
            continue
        key = str(job.get("key") or "")
        req_id = extract_workday_req_id(key) or extract_workday_req_id(str(job.get("url") or ""))
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


def _canonical_workday_key_for_hidden(hidden_key: str, jobs: list[dict]) -> str:
    req_id = extract_workday_req_id(hidden_key)
    if not req_id:
        return ""
    for job in jobs:
        key = str(job.get("key") or "")
        if key == hidden_key:
            continue
        if (job.get("source") or "").strip().lower() != "workday":
            continue
        if ":url:" in key:
            continue
        if extract_workday_req_id(key) == req_id:
            return key
    return ""


def _layout(title: str, body: str) -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f4ed;
      --panel: #fffdf8;
      --ink: #1f2937;
      --muted: #6b7280;
      --line: #ddd6c7;
      --good: #166534;
      --warn: #b45309;
      --bad: #991b1b;
      --accent: #0f766e;
      --button-ink: #ffffff;
      --field: #ffffff;
      --field-ink: #1f2937;
      --soft: #fbfaf6;
      --danger-panel: #fff7ed;
      --danger-line: #fdba74;
    }}
    [data-theme="dark"] {{
      color-scheme: dark;
      --bg: #0d1412;
      --panel: #151f1c;
      --ink: #e7efe9;
      --muted: #9aaca2;
      --line: #2a3934;
      --good: #86efac;
      --warn: #fbbf24;
      --bad: #fca5a5;
      --accent: #5eead4;
      --button-ink: #052e2b;
      --field: #101916;
      --field-ink: #e7efe9;
      --soft: #111a17;
      --danger-panel: #2a1b10;
      --danger-line: #92400e;
    }}
    body {{ margin: 0; font-family: Georgia, 'Times New Roman', serif; background: var(--bg); color: var(--ink); }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
    .nav {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 20px; }}
    .nav a {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; margin-bottom: 18px; }}
    .grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 18px; }}
    .hero {{ display: grid; grid-template-columns: 1.8fr 1fr; gap: 18px; align-items: start; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 16px 0; }}
    .stat {{ background: var(--soft); border: 1px solid var(--line); border-radius: 12px; padding: 14px; }}
    .stat strong {{ display: block; font-size: 1.6rem; line-height: 1.1; }}
    .stat span {{ color: var(--muted); font-size: 0.92rem; }}
    .helper-list {{ margin: 10px 0 0; padding-left: 18px; color: var(--muted); }}
    .table-wrap {{ overflow-x: auto; }}
    .muted {{ color: var(--muted); }}
    .score {{ font-weight: 700; }}
    .yes {{ color: var(--good); }}
    .maybe {{ color: var(--warn); }}
    .no {{ color: var(--bad); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    .section-row td {{
      background: color-mix(in srgb, var(--accent) 12%, var(--panel));
      border-top: 2px solid var(--line);
      border-bottom: 1px solid var(--line);
    }}
    textarea, input[type=text], input[type=url], select {{
      width: 100%; box-sizing: border-box; border: 1px solid var(--line);
      border-radius: 10px; padding: 10px 12px; font: inherit; background: var(--field); color: var(--field-ink);
    }}
    textarea {{ min-height: 220px; resize: vertical; }}
    button {{
      background: var(--accent); color: var(--button-ink); border: 0; border-radius: 999px;
      padding: 10px 16px; font: inherit; cursor: pointer; font-weight: 700;
    }}
    .theme-toggle {{
      margin-left: auto; background: transparent; color: var(--ink); border: 1px solid var(--line);
      padding: 7px 12px; font-size: 0.92rem;
    }}
    .pill {{
      display: inline-block; border: 1px solid var(--line); border-radius: 999px;
      padding: 4px 10px; margin-right: 6px; margin-bottom: 6px; background: var(--field);
    }}
    .split {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
    pre {{
      white-space: pre-wrap; word-break: break-word; background: var(--soft);
      border: 1px solid var(--line); border-radius: 10px; padding: 14px;
    }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
    .actions label {{ min-width: 140px; flex: 1 1 140px; }}
    .scan-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }}
    .scan-card {{ background: var(--soft); border: 1px solid var(--line); border-radius: 12px; padding: 14px; }}
    .scan-card h3 {{ margin: 0 0 8px; font-size: 1rem; }}
    .scan-card p {{ margin: 0; color: var(--muted); font-size: 0.95rem; }}
    .danger-card {{ background: var(--danger-panel); border-color: var(--danger-line); }}
    .danger-button {{ background: #b45309; color: #ffffff; }}
    .confirm-line {{ display: flex; align-items: center; gap: 8px; margin-top: 12px; color: var(--muted); font-size: 0.94rem; }}
    .confirm-line input[type=checkbox] {{ width: auto; }}
    .button-link {{
      display: inline-block; background: var(--accent); color: var(--button-ink); border: 0; border-radius: 999px;
      padding: 10px 16px; text-decoration: none; font-weight: 700;
    }}
    .artifact-actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0; }}
    .artifact-meta {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }}
    @media (max-width: 880px) {{
      .grid, .split, .hero, .scan-grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .theme-toggle {{ margin-left: 0; }}
      .wrap {{ padding: 16px; }}
    }}
  </style>
  <script>
    (function () {{
      const key = "jobRadarTheme";
      const saved = localStorage.getItem(key);
      const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      const theme = saved || (prefersDark ? "dark" : "light");
      document.documentElement.dataset.theme = theme;
    }})();
  </script>
</head>
<body>
  <div class="wrap">
    <div class="nav">
      <a href="/">Jobs</a>
      <a href="/health">Health</a>
      <a href="/boards">Board Health</a>
      <a href="/manual-jd">Paste JD</a>
      <a href="/settings">Feature Switchboard</a>
      <button type="button" id="theme-toggle" class="theme-toggle" aria-label="Toggle dark mode">Dark mode</button>
    </div>
    {body}
  </div>
  <script>
    (function () {{
      const key = "jobRadarTheme";
      const button = document.getElementById("theme-toggle");
      function applyTheme(theme) {{
        document.documentElement.dataset.theme = theme;
        if (button) {{
          button.textContent = theme === "dark" ? "Light mode" : "Dark mode";
          button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
        }}
      }}
      applyTheme(document.documentElement.dataset.theme || "light");
      if (button) {{
        button.addEventListener("click", function () {{
          const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
          localStorage.setItem(key, next);
          applyTheme(next);
        }});
      }}
    }})();
    window.copyText = function (id) {{
      const node = document.getElementById(id);
      if (!node) return;
      const text = node.textContent || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text);
        return;
      }}
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }};
    document.addEventListener("change", function (event) {{
      const target = event.target;
      if (!target || target.tagName !== "SELECT") return;
      const form = target.closest("form[data-autosubmit='true']");
      if (!form) return;
      if (typeof form.requestSubmit === "function") {{
        form.requestSubmit();
      }} else {{
        form.submit();
      }}
    }});
  </script>
</body>
</html>"""
    return html.encode("utf-8")


def serve_web(
    cfg: Config,
    db: Database,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    repo_root: str = "",
    auto_pull_interval_seconds: int = 300,
) -> None:
    defaults = _feature_defaults(cfg)
    db_lock = threading.RLock()
    scan_lock = threading.Lock()
    last_repo_pull_monotonic = 0.0
    github_sync_lock = threading.Lock()
    active_dashboard_db_path = str(_resolve_db_path(repo_root, cfg.database.path))
    active_dashboard_db_meta: dict[str, object] = {
        "path": active_dashboard_db_path,
        "strategy": "configured",
        "source": _dashboard_source_label(repo_root, Path(active_dashboard_db_path)),
        "dataset": "merged",
        "last_seen": "",
        "recent_jobs_24h": 0,
        "recent_first_seen_24h": 0,
        "total_jobs": 0,
    }
    scan_state: dict[str, object] = {
        "running": False,
        "mode": "",
        "message": "No scan has been started from the web UI yet.",
        "started_at": "",
        "finished_at": "",
    }
    public_db_note_logged = False
    current_dataset_preference = "merged"
    active_dashboard_source_signature = ""
    local_db_path = _resolve_db_path(repo_root, cfg.database.path)

    def _dashboard_source_signature(dataset_preference: str) -> str:
        root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
        primary = _resolve_db_path(repo_root, cfg.database.path)
        candidate_paths: list[Path] = [primary]
        candidate_paths.extend(path for path in _state_db_paths(root) if path != primary)
        candidate_paths.append(_github_archive_db_path(root))
        nested_public_export = (root / "public-export").resolve()
        candidate_paths.extend(path for path in _state_db_paths(nested_public_export) if path != primary)
        candidate_paths.append(_github_archive_db_path(nested_public_export))
        if root.name.lower() == "public-export":
            parent = root.parent.resolve()
            candidate_paths.extend(path for path in _state_db_paths(parent) if path != primary)
            candidate_paths.append(_github_archive_db_path(parent))

        requested = (dataset_preference or "merged").strip().lower()
        parts: list[str] = [requested]
        seen_paths: set[Path] = set()
        for path in candidate_paths:
            resolved = path.resolve()
            if resolved in seen_paths or not resolved.exists():
                continue
            seen_paths.add(resolved)
            try:
                stat = resolved.stat()
            except OSError:
                continue
            parts.append(f"{resolved}:{stat.st_mtime_ns}:{stat.st_size}")
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def _uses_public_db(meta: dict[str, object]) -> bool:
        source = str(meta.get("source") or "")
        strategy = str(meta.get("strategy") or "")
        return source == "public-export" or strategy.startswith("merged-public+")

    def _log_public_db_startup_note() -> None:
        nonlocal public_db_note_logged
        if public_db_note_logged or not _uses_public_db(active_dashboard_db_meta):
            return
        public_db_note_logged = True
        log.info(
            "Dashboard is using the public-export DB. Stop the dashboard before git pull, stash, rebase, or other git maintenance in public-export."
        )

    def _log_public_db_shutdown_note() -> None:
        if not _uses_public_db(active_dashboard_db_meta):
            return
        log.info(
            "Dashboard closed its public-export DB session. Git maintenance in public-export is safe again."
        )

    def _scan_snapshot() -> dict[str, object]:
        with scan_lock:
            return dict(scan_state)

    def _open_worker_db() -> Database:
        return Database(cfg.database.path)

    def _set_scan_state(**updates) -> None:
        with scan_lock:
            scan_state.update(updates)

    def _get_persistent_scan_value(name: str, default: str = "") -> str:
        worker_db = _open_worker_db()
        try:
            return worker_db.get_cursor_value(name, default)
        finally:
            worker_db.close()

    def _set_persistent_scan_value(name: str, value: str) -> None:
        worker_db = _open_worker_db()
        try:
            worker_db.set_cursor_value(name, value)
        finally:
            worker_db.close()

    def _dashboard_db_call(method_name: str, *args, **kwargs):
        with db_lock:
            return getattr(db, method_name)(*args, **kwargs)

    def _refresh_active_dashboard_job(seed_job: dict, thresholds) -> tuple[dict, object] | None:
        with db_lock:
            return _refresh_and_update_in_db(db, seed_job, thresholds)

    def _replace_dashboard_db(dataset_preference: str | None = None) -> None:
        nonlocal db, active_dashboard_db_path, active_dashboard_db_meta, public_db_note_logged, current_dataset_preference, active_dashboard_source_signature
        if dataset_preference is not None:
            current_dataset_preference = (dataset_preference or "merged").strip().lower()
        source_signature = _dashboard_source_signature(current_dataset_preference)
        replacement_path, strategy = _select_dashboard_db(repo_root, cfg.database.path, current_dataset_preference)
        resolved_path = str(replacement_path)
        snapshot = _db_snapshot(replacement_path)
        current_total_jobs = int(active_dashboard_db_meta.get("total_jobs") or 0)
        replacement_total_jobs = int(snapshot.get("total_jobs") or 0)
        same_dataset_refresh = str(active_dashboard_db_meta.get("dataset") or "") == current_dataset_preference
        if (
            same_dataset_refresh
            and resolved_path != active_dashboard_db_path
            and Path(active_dashboard_db_path).exists()
            and current_total_jobs >= 1000
            and replacement_total_jobs > 0
            and replacement_total_jobs < int(current_total_jobs * 0.8)
        ):
            log.warning(
                "Dashboard DB replacement skipped: candidate %s (%s, %s jobs) is much smaller than active %s (%s jobs).",
                resolved_path,
                strategy,
                replacement_total_jobs,
                active_dashboard_db_path,
                current_total_jobs,
            )
            return

        active_dashboard_db_meta = {
            "path": resolved_path,
            "strategy": strategy,
            "source": _dashboard_source_label(repo_root, replacement_path),
            "dataset": current_dataset_preference,
            "last_seen": snapshot.get("last_seen", ""),
            "recent_jobs_24h": snapshot.get("recent_jobs_24h", 0),
            "recent_first_seen_24h": snapshot.get("recent_first_seen_24h", 0),
            "total_jobs": snapshot.get("total_jobs", 0),
        }
        active_dashboard_source_signature = source_signature
        if resolved_path == active_dashboard_db_path:
            _log_public_db_startup_note()
            return
        replacement = Database(resolved_path)
        with db_lock:
            old_db = db
            db = replacement
            active_dashboard_db_path = resolved_path
        try:
            old_db.close()
        except Exception:
            pass
        public_db_note_logged = False
        log.info("Dashboard DB switched to %s (%s).", resolved_path, strategy)
        _log_public_db_startup_note()

    def _maybe_pull_repo() -> None:
        nonlocal last_repo_pull_monotonic
        if not repo_root or auto_pull_interval_seconds <= 0:
            return
        now = time.monotonic()
        if now - last_repo_pull_monotonic < auto_pull_interval_seconds:
            return
        last_repo_pull_monotonic = now
        if _scan_snapshot().get("running"):
            return
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception as exc:
            log.warning("Dashboard auto-pull skipped: git status failed (%s).", exc)
            return
        if status.returncode != 0 or status.stdout.strip():
            return
        try:
            pull = subprocess.run(
                ["git", "pull", "--ff-only", "origin", "main"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            log.warning("Dashboard auto-pull failed: %s", exc)
            return
        output = (pull.stdout or pull.stderr or "").strip()
        if pull.returncode != 0:
            log.warning("Dashboard auto-pull failed: %s", output or f"git exited with {pull.returncode}")
            return
        if "Already up to date." in output or "Already up-to-date." in output:
            _replace_dashboard_db(current_dataset_preference)
            return
        log.info("Dashboard auto-pull refreshed repo state.")
        _replace_dashboard_db(current_dataset_preference)

    def _sync_github_archives() -> tuple[bool, str]:
        if _scan_snapshot().get("running"):
            return False, "GitHub sync skipped because a scan is already running."
        if not github_sync_lock.acquire(blocking=False):
            return False, "GitHub sync is already in progress."
        github_archive_path = _github_archive_db_path(Path(repo_root).resolve() if repo_root else Path.cwd().resolve())
        try:
            for sidecar in (
                github_archive_path,
                github_archive_path.with_name(github_archive_path.name + "-wal"),
                github_archive_path.with_name(github_archive_path.name + "-shm"),
            ):
                try:
                    if sidecar.exists():
                        sidecar.unlink()
                except OSError:
                    pass
            Database(str(github_archive_path)).close()
        except Exception as exc:
            github_sync_lock.release()
            return False, f"GitHub archive DB setup failed: {exc}"
        temp_paths: list[Path] = []
        try:
            summaries: list[str] = []
            total_inserted = 0
            for source in GITHUB_SYNC_SOURCES:
                slug = str(source["slug"])
                url = str(source["url"])
                provenance_label = str(source.get("provenance") or "")
                temp_path = local_db_path.parent / f"github-sync-{slug}-{int(time.time() * 1000)}.db"
                temp_paths.append(temp_path)
                try:
                    _download_remote_db(url, temp_path)
                    stats = _import_missing_jobs_from_db(
                        github_archive_path,
                        temp_path,
                        prefer_overlay_scores=(slug == "rohith"),
                        provenance_label=provenance_label,
                    )
                except (OSError, sqlite3.Error, URLError) as exc:
                    log.warning("GitHub archive sync failed for %s: %s", source["name"], exc)
                    summaries.append(f"{source['name']}: failed")
                    continue
                inserted = int(stats.get("inserted") or 0)
                updated = int(stats.get("updated") or 0)
                total_inserted += inserted
                summaries.append(f"{source['name']}: +{inserted} new, {updated} enriched")
            _replace_dashboard_db(current_dataset_preference)
            if total_inserted > 0:
                return True, f"GitHub sync completed. {' | '.join(summaries)}"
            return True, f"GitHub sync completed with no new jobs added. {' | '.join(summaries)}"
        finally:
            for temp_path in temp_paths:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass
            github_sync_lock.release()

    def _start_scan(mode: str) -> bool:
        current = _scan_snapshot()
        if current.get("running"):
            return False

        started_at = datetime.now(timezone.utc).isoformat()
        batch_size = max(int(cfg.boards.batch_size or 0), 1)
        if mode == "boards":
            cursor_db = _open_worker_db()
            worker_db_cursor = cursor_db.get_cursor("boards_main")
            try:
                worker_db_cursor = int(worker_db_cursor or 0)
            except Exception:
                worker_db_cursor = 0
            finally:
                cursor_db.close()
            message = (
                f"Started next board batch at {started_at}. "
                f"It will scan up to {batch_size} boards from the current cursor ({worker_db_cursor})."
            )
        elif mode in {"broad_boards", "broad_all"}:
            cursor_db = _open_worker_db()
            worker_db_cursor = cursor_db.get_cursor(BROAD_NON_WORKDAY_CURSOR_KEY)
            try:
                worker_db_cursor = int(worker_db_cursor or 0)
            except Exception:
                worker_db_cursor = 0
            finally:
                cursor_db.close()
            if mode == "broad_all":
                message = (
                    f"Started full broad non-Workday sweep at {started_at}. "
                    f"It resumes from the broad cursor ({worker_db_cursor}) and wraps until all broad boards are covered."
                )
                _set_persistent_scan_value("last_broad_sweep_started_at", started_at)
            else:
                message = (
                    f"Started broad non-Workday board batch at {started_at}. "
                    f"It will scan up to {BROAD_NON_WORKDAY_BATCH_SIZE} Greenhouse/Lever/iCIMS boards "
                    f"from the broad cursor ({worker_db_cursor})."
                )
        elif mode == "all":
            cursor_db = _open_worker_db()
            worker_db_cursor = cursor_db.get_cursor("boards_main")
            try:
                worker_db_cursor = int(worker_db_cursor or 0)
            except Exception:
                worker_db_cursor = 0
            finally:
                cursor_db.close()
            message = (
                f"Started full board sweep at {started_at}. "
                f"It resumes from the current cursor ({worker_db_cursor}) and wraps until all boards are covered."
            )
            _set_persistent_scan_value("last_full_sweep_started_at", started_at)
        else:
            message = f"Started main sources scan at {started_at}."
        _set_scan_state(
            running=True,
            mode=mode,
            started_at=started_at,
            finished_at="",
            message=message,
        )

        def _worker() -> None:
            from .main import _resolve_boards_csv, build_notifier, run_boards, run_main

            _sync_runtime_feature_flags()
            worker_db = _open_worker_db()
            notifier = build_notifier(cfg)
            finished_at = ""
            message = ""
            try:
                if mode in {"main", "all"}:
                    run_main(cfg, worker_db, notifier, dry_run=False, no_notify=False, test_notify=False)
                if mode in {"boards", "all"}:
                    boards_csv = _resolve_boards_csv(cfg.boards.csv)
                    run_boards(
                        cfg,
                        worker_db,
                        notifier,
                        boards_csv=boards_csv,
                        batch_size=cfg.boards.batch_size,
                        timeout=cfg.boards.timeout,
                        workers=cfg.boards.workers,
                        dry_run=False,
                        no_notify=False,
                        test_notify=False,
                        run_until_wrap=(mode == "all"),
                        show_live_progress=False,
                    )
                if mode in {"broad_boards", "broad_all"}:
                    boards_csv = _resolve_boards_csv(BROAD_NON_WORKDAY_BOARDS_CSV)
                    run_boards(
                        cfg,
                        worker_db,
                        notifier,
                        boards_csv=boards_csv,
                        batch_size=BROAD_NON_WORKDAY_BATCH_SIZE,
                        timeout=cfg.boards.timeout,
                        workers=cfg.boards.workers,
                        dry_run=False,
                        no_notify=False,
                        test_notify=False,
                        notify_yes_only=True,
                        cursor_key=BROAD_NON_WORKDAY_CURSOR_KEY,
                        run_until_wrap=(mode == "broad_all"),
                        show_live_progress=False,
                    )
                finished_at = datetime.now(timezone.utc).isoformat()
                message = (
                    "Completed main sources + full board sweep from the saved cursor."
                    if mode == "all"
                    else "Completed main source scan."
                    if mode == "main"
                    else "Completed broad non-Workday board batch scan."
                    if mode == "broad_boards"
                    else "Completed full broad non-Workday sweep from the saved broad cursor."
                    if mode == "broad_all"
                    else "Completed next board batch scan."
                )
                if mode == "all":
                    _set_persistent_scan_value("last_full_sweep_finished_at", finished_at)
                if mode == "broad_all":
                    _set_persistent_scan_value("last_broad_sweep_finished_at", finished_at)
            except Exception as exc:
                finished_at = datetime.now(timezone.utc).isoformat()
                log.exception("Web-triggered scan failed: %s", exc)
                message = f"Scan failed at {finished_at}: {type(exc).__name__}: {exc}"
            finally:
                try:
                    worker_db.close()
                finally:
                    try:
                        _replace_dashboard_db(current_dataset_preference)
                    except Exception as exc:
                        log.warning("Dashboard DB refresh after scan failed: %s", exc)
                    if message.startswith("Scan failed at "):
                        _set_scan_state(
                            running=False,
                            finished_at=finished_at,
                            message=message,
                        )
                    else:
                        final_time = finished_at or datetime.now(timezone.utc).isoformat()
                        final_message = message or "Scan finished."
                        _set_scan_state(
                            running=False,
                            finished_at=final_time,
                            message=f"{final_message} Finished at {final_time}.",
                        )

        thread = threading.Thread(target=_worker, name=f"job-radar-scan-{mode}", daemon=True)
        thread.start()
        return True

    def _read_post(environ) -> dict[str, str]:
        try:
            size = int(environ.get("CONTENT_LENGTH") or "0")
        except ValueError:
            size = 0
        raw = environ["wsgi.input"].read(size).decode("utf-8") if size > 0 else ""
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: v[-1] if v else "" for k, v in parsed.items()}

    def _redirect(start_response, location: str):
        start_response("303 See Other", [("Location", location)])
        return [b""]

    def _html_headers() -> list[tuple[str, str]]:
        return [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
            ("Pragma", "no-cache"),
            ("Expires", "0"),
        ]

    def _artifact_content_type(format_name: str) -> str:
        normalized = (format_name or "").strip().lower()
        if normalized == "markdown":
            return "text/markdown; charset=utf-8"
        if normalized == "tex":
            return "application/x-tex; charset=utf-8"
        return "text/plain; charset=utf-8"

    def _packet_link(item: dict) -> str:
        if (item.get("format") or "").strip().lower() == "prompt_packet":
            return f"/packet?id={item['id']}"
        return f"/resume?id={item['id']}"

    def _flags() -> dict[str, bool]:
        return _dashboard_db_call("get_feature_flags", defaults)

    def _sync_runtime_feature_flags(flags: dict[str, bool] | None = None) -> dict[str, bool]:
        current = flags or _flags()
        # The switchboard can disable LLM scoring at runtime. Config/env still
        # owns enabling and API credentials so a stray checkbox cannot create calls.
        EVAL_CFG.llm_scoring.provider = cfg.llm_scoring.provider
        EVAL_CFG.llm_scoring.model = cfg.llm_scoring.model
        EVAL_CFG.llm_scoring.api_key = cfg.llm_scoring.api_key
        EVAL_CFG.llm_scoring.endpoint = cfg.llm_scoring.endpoint
        EVAL_CFG.llm_scoring.timeout = cfg.llm_scoring.timeout
        EVAL_CFG.llm_scoring.only_score_min = cfg.llm_scoring.only_score_min
        EVAL_CFG.llm_scoring.only_score_max = cfg.llm_scoring.only_score_max
        EVAL_CFG.llm_scoring.max_score_adjustment = cfg.llm_scoring.max_score_adjustment
        EVAL_CFG.llm_scoring.max_description_chars = cfg.llm_scoring.max_description_chars
        EVAL_CFG.llm_scoring.batch_size = cfg.llm_scoring.batch_size
        EVAL_CFG.llm_scoring.max_calls_per_process = cfg.llm_scoring.max_calls_per_process
        EVAL_CFG.llm_scoring.max_daily_calls = cfg.llm_scoring.max_daily_calls
        EVAL_CFG.llm_scoring.enabled = bool(cfg.llm_scoring.enabled and current.get("llm_scoring", cfg.llm_scoring.enabled))
        return current

    def _set_feature_flag_everywhere(name: str, enabled: bool) -> None:
        _dashboard_db_call("set_feature_flag", name, enabled)
        for path, writer in _open_backing_dbs():
            try:
                writer.set_feature_flag(name, enabled)
            except Exception as exc:
                log.debug("Failed to save feature flag %s to backing DB %s: %s", name, path, exc)
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

    def _label_thresholds():
        return calibrate_thresholds(_dashboard_db_call("get_feedback_jobs"))

    def _backing_db_paths() -> list[str]:
        root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
        primary = str(_resolve_db_path(repo_root, cfg.database.path))
        public_root = (root / "public-export").resolve()
        public_state_paths = {str(path) for path in _state_db_paths(public_root) if path.exists()}
        if root.name.lower() == "public-export":
            public_state_paths.update(str(path) for path in _state_db_paths(root) if path.exists())
        paths: list[str] = []
        if primary not in public_state_paths:
            paths.append(primary)
        paths.extend(
            str(path)
            for path in _state_db_paths(root)
            if str(path) != primary and str(path) not in public_state_paths and path.exists()
        )
        deduped: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            deduped.append(path)
            seen.add(path)
        return deduped

    def _open_backing_dbs() -> list[tuple[str, Database]]:
        opened: list[tuple[str, Database]] = []
        for path in _backing_db_paths():
            try:
                opened.append((path, Database(path)))
            except Exception as exc:
                log.warning("Skipping backing DB %s for dashboard write: %s", path, exc)
        return opened

    def _evaluate_for_row(job_row: dict, thresholds, *, use_llm: bool = True) -> object:
        _sync_runtime_feature_flags()
        return evaluate_job(
            job_row["title"],
            job_row.get("description", ""),
            company=job_row["company"],
            location=job_row["location"],
            source=job_row.get("source", ""),
            require_us_location=cfg.filter.require_us_location,
            yes_threshold=thresholds.yes,
            maybe_threshold=thresholds.maybe,
            use_llm=use_llm,
        )

    def _apply_llm_review_to_evaluation(evaluation: EvaluationResult, review, thresholds) -> EvaluationResult:
        if not review or not getattr(review, "used", False):
            return evaluation
        adjusted_score = apply_llm_delta(
            score=evaluation.score,
            critical_skill_gaps=evaluation.critical_skill_gaps,
            review=review,
            max_adjustment=EVAL_CFG.llm_scoring.max_score_adjustment,
        )
        evaluation.llm_review = review.to_dict()
        if adjusted_score != evaluation.score:
            evaluation.score = adjusted_score
            evaluation.grade = _score_to_grade(evaluation.score)
            evaluation.label = label_for_score(
                evaluation.score,
                yes_threshold=thresholds.yes,
                maybe_threshold=thresholds.maybe,
            )
            if review.reason:
                evaluation.reasons.insert(0, f"LLM reviewer adjusted score by {review.score_delta}: {review.reason}")
        elif review.reason:
            evaluation.reasons.append(f"LLM reviewer kept score unchanged: {review.reason}")
        evaluation.reasons = evaluation.reasons[:4]
        evaluation.fit_summary = f"Grade {evaluation.grade} ({evaluation.score}/100). " + " ".join(evaluation.reasons[:3])
        return evaluation

    def _refresh_and_update_in_db(target_db: Database, seed_job: dict, thresholds) -> tuple[dict, object] | None:
        existing = target_db.get_job(seed_job["key"])
        if existing is None:
            return None
        target_db.refresh_job_intelligence(
            key=seed_job["key"],
            source=seed_job["source"],
            company=seed_job["company"],
            title=seed_job["title"],
            location=seed_job["location"],
            url=seed_job["url"],
            description=seed_job.get("description", ""),
        )
        refreshed = target_db.get_job(seed_job["key"]) or seed_job
        evaluation = _evaluate_for_row(refreshed, thresholds)
        current_json = refreshed.get("evaluation_json") or ""
        if (
            evaluation.fit_summary != (refreshed.get("fit_summary") or "")
            or evaluation.score != refreshed["score"]
            or evaluation.label != refreshed["label"]
            or evaluation.grade != (refreshed.get("grade") or "")
            or evaluation.to_json() != current_json
        ):
            target_db.update_job_evaluation(
                key=seed_job["key"],
                score=evaluation.score,
                label=evaluation.label,
                grade=evaluation.grade,
                evaluation_json=evaluation.to_json(),
                fit_summary=evaluation.fit_summary,
                description=refreshed.get("description", ""),
            )
            refreshed = target_db.get_job(seed_job["key"]) or refreshed
        return refreshed, evaluation

    def _mark_job_viewed(key: str) -> None:
        if not key:
            return
        if not _uses_public_db(active_dashboard_db_meta):
            try:
                _dashboard_db_call("mark_job_viewed", key)
            except Exception as exc:
                log.debug("Failed to mark active dashboard job viewed: %s", exc)
        for path, writer in _open_backing_dbs():
            try:
                writer.mark_job_viewed(key)
            except Exception as exc:
                log.debug("Failed to mark job viewed in backing DB %s: %s", path, exc)
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

    def _persist_evaluation(job: dict) -> tuple[dict, object]:
        stored_evaluation = _evaluation_from_payload(job)
        if stored_evaluation is not None:
            return job, stored_evaluation
        thresholds = _label_thresholds()
        writers = _open_backing_dbs()
        writer_paths = {path for path, _ in writers}
        chosen_job = dict(job)
        chosen_eval = _evaluate_for_row(chosen_job, thresholds)
        chosen_job["score"] = chosen_eval.score
        chosen_job["label"] = chosen_eval.label
        chosen_job["grade"] = chosen_eval.grade
        chosen_job["fit_summary"] = chosen_eval.fit_summary
        chosen_job["evaluation_json"] = chosen_eval.to_json()
        try:
            if active_dashboard_db_path not in writer_paths:
                active_result = _refresh_active_dashboard_job(job, thresholds)
                if active_result is not None:
                    refreshed_job, refreshed_eval = active_result
                    chosen_job = refreshed_job
                    chosen_eval = refreshed_eval
            for path, writer in writers:
                try:
                    _refresh_and_update_in_db(writer, job, thresholds)
                except Exception as exc:
                    log.warning("Skipped evaluation persist to backing DB %s: %s", path, exc)
            return chosen_job, chosen_eval
        finally:
            for path, writer in writers:
                try:
                    writer.close()
                except Exception:
                    log.debug("Failed to close backing DB %s after evaluation persist.", path)

    def _current_view_jobs(filters: dict[str, str]) -> tuple[list[dict], int, list[dict], set[str]]:
        days_raw = (filters.get("days") or str(RECENT_JOB_DAYS)).strip()
        queue = (filters.get("queue") or "active").strip().lower()
        dataset = (filters.get("dataset") or current_dataset_preference or "merged").strip().lower()
        status = (filters.get("status") or "all").strip().lower()
        source = (filters.get("source") or "all").strip().lower()
        sort_by = (filters.get("sort") or "newest").strip().lower()
        min_score_raw = (filters.get("min_score") or "0").strip().lower()
        days = _days_value_to_max_days(days_raw)
        try:
            min_score = max(int(min_score_raw), 0)
        except ValueError:
            min_score = 0

        board_jobs = _dashboard_db_call("list_jobs_for_board", limit=None)
        thresholds = _label_thresholds()
        board_jobs = [
            _display_job_with_consistent_evaluation(job, thresholds)
            for job in board_jobs
        ]
        filtered_jobs = [
            job for job in board_jobs
            if _dataset_match(job, dataset)
            and _queue_match(job, queue, status)
            and _passes_freshness(job, days_raw=days_raw, max_days=days)
            and _source_match(job, source)
            and _score_match(job, min_score)
        ]
        hidden_non_us_count = 0
        if cfg.filter.require_us_location:
            hidden_non_us_count = sum(
                1 for job in filtered_jobs
                if not _location_allowed_for_review(job.get("location", ""), require_us_location=True)
            )
            filtered_jobs = [
                job for job in filtered_jobs
                if _location_allowed_for_review(job.get("location", ""), require_us_location=True)
            ]

        all_jobs = _dedupe_board_jobs(filtered_jobs)
        hidden_workday_keys = _workday_legacy_duplicate_keys(all_jobs)
        jobs = [
            job for job in all_jobs
            if job.get("key") not in hidden_workday_keys
        ]
        _sort_jobs(jobs, sort_by)
        return jobs, hidden_non_us_count, all_jobs, hidden_workday_keys

    def _filtered_jobs_snapshot(filters: dict[str, str], *, limit: int | None = 500) -> list[dict]:
        jobs, _, _, _ = _current_view_jobs(filters)
        if limit is None:
            return jobs
        return jobs[: max(limit, 1)]

    def _batch_refresh(jobs: list[dict], *, active_only: bool = False) -> int:
        thresholds = _label_thresholds()
        writers = [] if active_only else _open_backing_dbs()
        writer_paths = {path for path, _ in writers}
        # Web batch re-score writes to the disposable active dashboard DB, so
        # it should refresh imported rows too. Otherwise the list can show a
        # stale YES while the detail page recalculates the same job as MAYBE.
        eligible_jobs = [
            job for job in jobs
            if job is not None and (active_only or _batch_rescore_allowed(job))
        ]
        if active_only:
            _sync_runtime_feature_flags()
            refreshed_jobs: list[dict] = []
            with db_lock:
                for job in eligible_jobs:
                    existing = db.get_job(job["key"])
                    if existing is None:
                        continue
                    db.refresh_job_intelligence(
                        key=job["key"],
                        source=job["source"],
                        company=job["company"],
                        title=job["title"],
                        location=job["location"],
                        url=job["url"],
                        description=job.get("description", ""),
                    )
                    refreshed_jobs.append(db.get_job(job["key"]) or job)

            evaluated_jobs: list[tuple[dict, EvaluationResult]] = [
                (job, _evaluate_for_row(job, thresholds, use_llm=False))
                for job in refreshed_jobs
            ]
            reviews = {}
            if EVAL_CFG.llm_scoring.enabled:
                batch_size = max(1, min(20, int(getattr(EVAL_CFG.llm_scoring, "batch_size", 10) or 10)))
                chunk_count = 0
                for idx in range(0, len(evaluated_jobs), batch_size):
                    chunk = evaluated_jobs[idx: idx + batch_size]
                    chunk_count += 1
                    reviews.update(
                        review_jobs_batch(
                            cfg=EVAL_CFG,
                            jobs=[
                                {
                                    "key": job["key"],
                                    "title": job.get("title", ""),
                                    "company": job.get("company", ""),
                                    "location": job.get("location", ""),
                                    "description": job.get("description", ""),
                                    "deterministic_payload": evaluation.to_dict(),
                                }
                                for job, evaluation in chunk
                            ],
                        )
                    )
                log.info(
                    "LLM batch re-score: candidates=%s, batch_size=%s, chunks=%s, reviews=%s",
                    len(evaluated_jobs),
                    batch_size,
                    chunk_count,
                    len(reviews),
                )

            with db_lock:
                for job, evaluation in evaluated_jobs:
                    evaluation = _apply_llm_review_to_evaluation(evaluation, reviews.get(job["key"]), thresholds)
                    db.update_job_evaluation(
                        key=job["key"],
                        score=evaluation.score,
                        label=evaluation.label,
                        grade=evaluation.grade,
                        evaluation_json=evaluation.to_json(),
                        fit_summary=evaluation.fit_summary,
                        description=job.get("description", ""),
                    )
            return len(evaluated_jobs)
        try:
            for job in eligible_jobs:
                wrote_active = False
                if active_only or active_dashboard_db_path not in writer_paths:
                    result = _refresh_active_dashboard_job(job, thresholds)
                    if result is not None:
                        wrote_active = True
                for _, writer in writers:
                    _refresh_and_update_in_db(writer, job, thresholds)
                if not wrote_active and not writers:
                    _refresh_active_dashboard_job(job, thresholds)
            return len(eligible_jobs)
        finally:
            for path, writer in writers:
                try:
                    writer.close()
                except Exception:
                    log.debug("Failed to close backing DB %s after batch refresh.", path)

    def _dedupe_board_jobs(rows: list[dict]) -> list[dict]:
        best: dict[tuple[str, ...], dict] = {}
        for row in rows:
            canonical_key = (row.get("canonical_key") or "").strip().lower()
            normalized_url = re.sub(r"[?#].*$", "", (row.get("url") or "").strip().lower()).rstrip("/")
            if canonical_key:
                fingerprint = ("canonical", canonical_key)
            elif normalized_url:
                fingerprint = ("url", normalized_url)
            else:
                fingerprint = (
                    "fallback",
                    (row.get("source") or "").strip().lower(),
                    (row.get("company") or "").strip().lower(),
                    (row.get("title") or "").strip().lower(),
                    (row.get("location") or "").strip().lower(),
                )
            current = best.get(fingerprint)
            if current is None:
                best[fingerprint] = row
                continue
            current_desc_len = len((current.get("description") or "").strip())
            row_desc_len = len((row.get("description") or "").strip())
            current_stamp = current.get("last_seen") or current.get("first_seen") or ""
            row_stamp = row.get("last_seen") or row.get("first_seen") or ""
            if row_desc_len > current_desc_len or (row_desc_len == current_desc_len and row_stamp > current_stamp):
                best[fingerprint] = row
        return list(best.values())

    def _jobs_page(start_response, query: dict[str, list[str]]):
        days_raw = (query.get("days") or [str(RECENT_JOB_DAYS)])[-1]
        queue = ((query.get("queue") or ["active"])[-1] or "active").strip().lower()
        dataset_choice = ((query.get("dataset") or [current_dataset_preference])[-1] or current_dataset_preference).strip().lower()
        status = ((query.get("status") or ["all"])[-1] or "all").strip().lower()
        source = ((query.get("source") or ["all"])[-1] or "all").strip().lower()
        sort_by = ((query.get("sort") or ["newest"])[-1] or "newest").strip().lower()
        min_score_raw = ((query.get("min_score") or ["0"])[-1] or "0").strip().lower()
        rescore_limit_raw = (query.get("rescore_limit") or ["500"])[-1]
        page_message = ((query.get("message") or [""])[-1] or "").strip()
        days = _days_value_to_max_days(days_raw)
        try:
            min_score = max(int(min_score_raw), 0)
        except ValueError:
            min_score = 0
            min_score_raw = "0"
        if rescore_limit_raw == "all":
            rescore_limit = None
        else:
            try:
                rescore_limit = max(int(rescore_limit_raw), 1)
            except ValueError:
                rescore_limit = 500
                rescore_limit_raw = "500"
        jobs, hidden_non_us_count, all_jobs, hidden_workday_keys = _current_view_jobs(
            {
                "days": days_raw,
                "queue": queue,
                "dataset": dataset_choice,
                "status": status,
                "source": source,
                "sort": sort_by,
                "min_score": str(min_score),
            }
        )
        source_names = sorted({(job.get("source") or "").strip().lower() for job in jobs if (job.get("source") or "").strip()})
        rows: list[str] = []
        current_fetch_bucket = ""
        show_fetch_buckets = sort_by in {"newest", "fetched"}
        for job in jobs:
            fetch_bucket = _fetched_bucket_label(job)
            if show_fetch_buckets and fetch_bucket != current_fetch_bucket:
                current_fetch_bucket = fetch_bucket
                rows.append(
                    "<tr class=\"section-row\">"
                    f"<td colspan=\"11\"><strong>{escape(fetch_bucket)}</strong> "
                    "<span class=\"muted\">unviewed jobs are listed first inside this fetched group</span></td>"
                    "</tr>"
                )
            label = escape(job["label"])
            grade = escape(job.get("grade") or "F")
            pipeline_status = escape(job.get("pipeline_status") or "new")
            fetched_text = _format_local_dt(job.get("first_seen")) or "unknown"
            viewed_text = _format_local_dt(job.get("viewed_at"))
            fetched_html = (
                f"<div>{escape(fetched_text)}</div>"
                + (f"<div class=\"muted\">Viewed {escape(viewed_text)}</div>" if viewed_text else "<div class=\"pill\">Unviewed</div>")
            )
            dataset_href = quote(dataset_choice, safe="")
            role_bits = [
                f"<a href=\"/job?key={quote(job['key'], safe='')}&dataset={dataset_href}\">{escape(job['title'])}</a>",
                f"<div class=\"muted\">{escape(job['company'])}</div>",
            ]
            if int(job.get("is_repost") or 0):
                role_bits.append("<div><span class=\"pill\">Likely repost</span></div>")
            quality_score = int(job.get("employer_quality_score") or 0)
            rows.append(
                "<tr>"
                f"<td><input type=\"checkbox\" name=\"job_key\" value=\"{escape(job['key'])}\"></td>"
                f"<td>{''.join(role_bits)}</td>"
                f"<td>{escape(job['location'])}</td>"
                f"<td><span class=\"score {label}\">{job['score']}</span></td>"
                f"<td>{grade}</td>"
                f"<td class=\"{label}\">{label.upper()}</td>"
                f"<td>{fetched_html}</td>"
                f"<td>{quality_score}</td>"
                f"<td>{_structured_signal_pills(job)}</td>"
                f"<td>{pipeline_status}</td>"
                f"<td>{escape(job['source'])}</td>"
                "</tr>"
            )
        rows_html = "".join(rows) if rows else "<tr><td colspan=\"11\">No jobs match the current filters.</td></tr>"
        queue_options = "".join(
            f"<option value=\"{name}\"{' selected' if queue == name else ''}>{label}</option>"
            for name, label in (("active", "Active"), ("actionable", "Actionable"), ("all", "All"))
        )
        status_options = "".join(
            f"<option value=\"{name}\"{' selected' if status == name else ''}>{label}</option>"
            for name, label in (("all", "All statuses"),) + tuple((item, item.replace('_', ' ').title()) for item in PIPELINE_STATUSES)
        )
        source_options = "".join(
            f"<option value=\"{escape(name)}\"{' selected' if source == name else ''}>{escape(name.title())}</option>"
            for name in ["all", *source_names]
        )
        dataset_options = "".join(
            f"<option value=\"{value}\"{' selected' if dataset_choice == value else ''}>{label}</option>"
            for value, label in (
                ("merged", "Merged (Recommended)"),
                ("merged-all", "Merged (All)"),
                ("local", "Local"),
                ("github-mine", "GitHub vasishta02"),
                ("github-rohith", "GitHub Rohith"),
                ("github", "GitHub Archive"),
                ("public", "Public"),
                ("auto", "Auto Fallback"),
            )
        )
        day_options = "".join(
            f"<option value=\"{value}\"{' selected' if days_raw == value else ''}>{label}</option>"
            for value, label in (
                ("posted_1", "24 hours (posted only)"),
                ("1", "24 hours (posted or first seen)"),
                ("seen_1", "24 hours (first seen)"),
                ("3", "3 days"),
                ("7", "7 days"),
                ("14", "14 days"),
                ("all", "All"),
            )
        )
        sort_options = "".join(
            f"<option value=\"{name}\"{' selected' if sort_by == name else ''}>{label}</option>"
            for name, label in (
                ("newest", "Newest"),
                ("posted", "Posted Newest"),
                ("oldest", "Oldest"),
                ("score", "Score"),
                ("rating", "Rating"),
                ("source", "Source"),
            )
        )
        min_score_options = "".join(
            f"<option value=\"{value}\"{' selected' if min_score_raw == value else ''}>{label}</option>"
            for value, label in (
                ("0", "All scores"),
                ("60", "60+"),
                ("70", "70+"),
            )
        )
        scan = _scan_snapshot()
        scan_mode = escape(str(scan.get("mode") or ""))
        scan_message = escape(str(scan.get("message") or ""))
        scan_started = escape(str(scan.get("started_at") or ""))
        scan_finished = escape(str(scan.get("finished_at") or ""))
        scan_state_label = "Running" if scan.get("running") else "Idle"
        last_full_sweep_finished = escape(_get_persistent_scan_value("last_full_sweep_finished_at"))
        last_full_sweep_started = escape(_get_persistent_scan_value("last_full_sweep_started_at"))
        last_broad_sweep_finished = escape(_get_persistent_scan_value("last_broad_sweep_finished_at"))
        last_broad_sweep_started = escape(_get_persistent_scan_value("last_broad_sweep_started_at"))
        boards_cursor_updated_at = escape(_get_persistent_scan_value("boards_main_updated_at"))
        broad_boards_cursor_updated_at = escape(_get_persistent_scan_value(f"{BROAD_NON_WORKDAY_CURSOR_KEY}_updated_at"))
        board_total = _board_inventory_total(cfg)
        broad_board_total = _board_inventory_total(cfg, BROAD_NON_WORKDAY_BOARDS_CSV)
        try:
            boards_cursor = int(_get_persistent_scan_value("boards_main", "0") or 0)
        except Exception:
            boards_cursor = 0
        try:
            broad_boards_cursor = int(_get_persistent_scan_value(BROAD_NON_WORKDAY_CURSOR_KEY, "0") or 0)
        except Exception:
            broad_boards_cursor = 0
        shown_yes = sum(1 for job in jobs if (job.get("label") or "").strip().lower() == "yes")
        shown_maybe = sum(1 for job in jobs if (job.get("label") or "").strip().lower() == "maybe")
        shown_no = sum(1 for job in jobs if (job.get("label") or "").strip().lower() == "no")
        active_sources = len({(job.get("source") or "").strip().lower() for job in jobs if (job.get("source") or "").strip()})
        db_meta = dict(active_dashboard_db_meta)
        db_source = escape(str(db_meta.get("source") or "local"))
        db_strategy = escape(str(db_meta.get("strategy") or "configured"))
        db_path_label = escape(str(db_meta.get("path") or ""))
        db_last_seen = escape(str(db_meta.get("last_seen") or "unknown"))
        db_recent_jobs = int(db_meta.get("recent_jobs_24h") or 0)
        db_recent_first_seen_jobs = int(db_meta.get("recent_first_seen_24h") or 0)
        db_total_jobs = int(db_meta.get("total_jobs") or 0)
        hidden_notes: list[str] = []
        if hidden_workday_keys:
            hidden_notes.append(f"{len(hidden_workday_keys)} legacy Workday duplicate(s)")
        if hidden_non_us_count:
            hidden_notes.append(f"{hidden_non_us_count} non-US job(s)")
        hidden_workday_note = (
            f"<p class=\"muted\">Hidden automatically: {', '.join(hidden_notes)}.</p>"
            if hidden_notes
            else ""
        )
        _, _, intake_scope_jobs, _ = _current_view_jobs(
            {
                "days": "all",
                "queue": queue,
                "dataset": dataset_choice,
                "status": status,
                "source": source,
                "sort": sort_by,
                "min_score": str(min_score),
            }
        )
        recent_alert_rows = _recent_alert_summary_rows(intake_scope_jobs, days=3)
        recent_alerts_html = (
            "<div class=\"card\">"
            "<h2>Recent Intake</h2>"
            "<table><thead><tr><th>Day</th><th>New jobs</th><th>Yes</th><th>Maybe</th><th>Review pool</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td>{escape(str(row['day']))}</td>"
                f"<td>{int(row['total'])}</td>"
                f"<td>{int(row['yes'])}</td>"
                f"<td>{int(row['maybe'])}</td>"
                f"<td>{int(row['review'])}</td>"
                "</tr>"
                for row in recent_alert_rows
            )
            + "</tbody></table>"
            "<p class=\"muted\">This is based on job <strong>first_seen</strong> dates in the current dataset/filter scope and grouped by your local timezone, so it gives us a quick day-by-day view of new intake and likely alert volume.</p>"
            "</div>"
        ) if recent_alert_rows else ""
        page_message_html = f"<p><strong>{escape(page_message)}</strong></p>" if page_message else ""
        rescore_options = "".join(
            f"<option value=\"{value}\"{' selected' if rescore_limit_raw == value else ''}>{label}</option>"
            for value, label in (("100", "100 jobs"), ("250", "250 jobs"), ("500", "500 jobs"), ("1000", "1000 jobs"), ("all", "All jobs"))
        )
        body = (
            "<div class=\"card\">"
            "<div class=\"hero\">"
            "<div>"
            "<h1>Job Review Board</h1>"
            "<p class=\"muted\">Review fresh roles, update your pipeline quickly, and run scans without leaving the page.</p>"
            f"<p class=\"muted\">Dashboard data source: <strong>{db_source}</strong> | strategy: <strong>{db_strategy}</strong></p>"
            f"<p class=\"muted\">Active DB: <code>{db_path_label}</code></p>"
            f"<p class=\"muted\">DB freshness: last seen <strong>{db_last_seen}</strong> | recent jobs by posted/first-seen (24h): <strong>{db_recent_jobs}</strong> | recent jobs by first-seen only (24h): <strong>{db_recent_first_seen_jobs}</strong> | total stored: <strong>{db_total_jobs}</strong></p>"
            "<ul class=\"helper-list\">"
            "<li><strong>Scan Main Sources</strong> is the fastest local refresh for direct high-priority companies.</li>"
            "<li><strong>Scan Next Board Batch</strong> is the recommended local board action when you just want a quick incremental update.</li>"
            "<li><strong>Scan Full Board Sweep</strong> is intentionally gated because it can take a while and is usually better left to GitHub Actions.</li>"
            "</ul>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Scanner Status</h2>"
            f"<p class=\"muted\">State: <strong>{scan_state_label}</strong>"
            + (f" | Mode: <strong>{scan_mode}</strong>" if scan_mode else "")
            + "</p>"
            f"<p class=\"muted\">{scan_message}</p>"
            + (
                f"<p class=\"muted\">Last full sweep: <strong>{last_full_sweep_finished}</strong></p>"
                if last_full_sweep_finished
                else f"<p class=\"muted\">Last full sweep started: <strong>{last_full_sweep_started}</strong> (no completion recorded yet)</p>"
                if last_full_sweep_started
                else ""
            )
            + (f"<p class=\"muted\">Started: {scan_started}</p>" if scan_started else "")
            + (f"<p class=\"muted\">Finished: {scan_finished}</p>" if scan_finished else "")
            + f"<p class=\"muted\">Resume point: <strong>{boards_cursor}</strong> / {board_total or 'unknown'}</p>"
            + f"<p class=\"muted\">Broad non-Workday resume point: <strong>{broad_boards_cursor}</strong> / {broad_board_total or 'unknown'}</p>"
            + (
                f"<p class=\"muted\">Last broad non-Workday sweep: <strong>{last_broad_sweep_finished}</strong></p>"
                if last_broad_sweep_finished
                else f"<p class=\"muted\">Last broad non-Workday sweep started: <strong>{last_broad_sweep_started}</strong> (no completion recorded yet)</p>"
                if last_broad_sweep_started
                else ""
            )
            + (
                f"<p class=\"muted\">Last saved cursor update: <strong>{boards_cursor_updated_at}</strong></p>"
                if boards_cursor_updated_at
                else ""
            )
            + (
                f"<p class=\"muted\">Last broad cursor update: <strong>{broad_boards_cursor_updated_at}</strong></p>"
                if broad_boards_cursor_updated_at
                else ""
            )
            + "</div>"
            "</div>"
            "<div class=\"stats\">"
            f"<div class=\"stat\"><strong>{len(jobs)}</strong><span>Jobs shown</span></div>"
            f"<div class=\"stat\"><strong>{shown_yes}</strong><span>Yes matches</span></div>"
            f"<div class=\"stat\"><strong>{shown_maybe}</strong><span>Maybe matches</span></div>"
            f"<div class=\"stat\"><strong>{shown_no}</strong><span>No matches</span></div>"
            "</div>"
            f"<p class=\"muted\">Showing {len(jobs)} jobs from {len(all_jobs)} stored roles across {active_sources} source(s) in the current view.</p>"
            f"{recent_alerts_html}"
            f"{page_message_html}"
            f"{hidden_workday_note}"
            "<div class=\"card\">"
            "<h2>Run Scanner</h2>"
            "<form method=\"post\" action=\"/scan\" class=\"actions\">"
            f"<input type=\"hidden\" name=\"days\" value=\"{escape(days_raw)}\">"
            f"<input type=\"hidden\" name=\"queue\" value=\"{escape(queue)}\">"
            f"<input type=\"hidden\" name=\"dataset\" value=\"{escape(dataset_choice)}\">"
            f"<input type=\"hidden\" name=\"source\" value=\"{escape(source)}\">"
            f"<input type=\"hidden\" name=\"status\" value=\"{escape(status)}\">"
            f"<input type=\"hidden\" name=\"sort\" value=\"{escape(sort_by)}\">"
            f"<input type=\"hidden\" name=\"min_score\" value=\"{escape(str(min_score_raw))}\">"
            f"<input type=\"hidden\" name=\"rescore_limit\" value=\"{escape(rescore_limit_raw)}\">"
            "<div class=\"scan-grid\">"
            "<div class=\"scan-card\">"
            "<h3>Scan Main Sources</h3>"
            "<p>Fastest local refresh for core companies and direct sources.</p>"
            "<div class=\"actions\"><button type=\"submit\" name=\"scan_mode\" value=\"main\">Run Main Sources</button></div>"
            "</div>"
            "<div class=\"scan-card\">"
            "<h3>Scan Next Board Batch</h3>"
            "<p>Recommended local board refresh. Advances the saved cursor without doing a full wrap.</p>"
            "<div class=\"actions\"><button type=\"submit\" name=\"scan_mode\" value=\"boards\">Run Next Board Batch</button></div>"
            "</div>"
            "<div class=\"scan-card\">"
            "<h3>Scan Broad Non-Workday Batch</h3>"
            f"<p>Extra Greenhouse/Lever/iCIMS coverage from the broad lane. Scans up to {BROAD_NON_WORKDAY_BATCH_SIZE} boards and advances a separate broad cursor.</p>"
            "<div class=\"actions\"><button type=\"submit\" name=\"scan_mode\" value=\"broad_boards\">Run Broad Batch</button></div>"
            "</div>"
            "<div class=\"scan-card danger-card\">"
            "<h3>Scan All Broad Non-Workday</h3>"
            f"<p>Walks the full broad non-Workday lane from the saved broad cursor until all {broad_board_total or 'known'} boards are covered.</p>"
            "<label class=\"confirm-line\"><input type=\"checkbox\" name=\"confirm_broad_sweep\" value=\"1\">I really want a full broad sweep</label>"
            "<div class=\"actions\"><button type=\"submit\" name=\"scan_mode\" value=\"broad_all\" class=\"danger-button\">Run Full Broad Sweep</button></div>"
            "</div>"
            "<div class=\"scan-card danger-card\">"
            "<h3>Scan Full Board Sweep</h3>"
            "<p>Slowest option. Walks the cursor until every board is covered, so use it only when you explicitly want a full local pass.</p>"
            "<label class=\"confirm-line\"><input type=\"checkbox\" name=\"confirm_full_sweep\" value=\"1\">I really want a full local sweep</label>"
            "<div class=\"actions\"><button type=\"submit\" name=\"scan_mode\" value=\"all\" class=\"danger-button\">Run Full Board Sweep</button></div>"
            "</div>"
            "</div>"
            "</form>"
            "<p class=\"muted\">For day-to-day use, pull the latest repo data, open the web UI, and use either Main Sources or Next Board Batch. Full sweeps are better as occasional manual actions.</p>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Update from GitHub</h2>"
            "<form method=\"post\" action=\"/github-sync\" class=\"actions\">"
            f"<input type=\"hidden\" name=\"days\" value=\"{escape(days_raw)}\">"
            f"<input type=\"hidden\" name=\"queue\" value=\"{escape(queue)}\">"
            f"<input type=\"hidden\" name=\"dataset\" value=\"{escape(dataset_choice)}\">"
            f"<input type=\"hidden\" name=\"source\" value=\"{escape(source)}\">"
            f"<input type=\"hidden\" name=\"status\" value=\"{escape(status)}\">"
            f"<input type=\"hidden\" name=\"sort\" value=\"{escape(sort_by)}\">"
            f"<input type=\"hidden\" name=\"min_score\" value=\"{escape(str(min_score_raw))}\">"
            f"<input type=\"hidden\" name=\"rescore_limit\" value=\"{escape(rescore_limit_raw)}\">"
            "<button type=\"submit\">Pull GitHub Job Data</button>"
            "</form>"
            "<p class=\"muted\">Downloads the committed job snapshots from your GitHub repo and Rohith's GitHub repo, then adds or enriches jobs in the separate <strong>GitHub Archive</strong> dataset.</p>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Filter Jobs</h2>"
            "<form method=\"get\" action=\"/\" class=\"actions\" data-autosubmit=\"true\">"
            f"<label>Queue<br><select name=\"queue\">{queue_options}</select></label>"
            f"<label>Dataset<br><select name=\"dataset\">{dataset_options}</select></label>"
            f"<label>Freshness<br><select name=\"days\">{day_options}</select></label>"
            f"<label>Source<br><select name=\"source\">{source_options}</select></label>"
            f"<label>Status<br><select name=\"status\">{status_options}</select></label>"
            f"<label>Sort<br><select name=\"sort\">{sort_options}</select></label>"
            f"<label>Minimum Score<br><select name=\"min_score\">{min_score_options}</select></label>"
            "<button type=\"submit\">Apply Filters</button>"
            "</form>"
            "<form method=\"post\" action=\"/jobs/re-evaluate\" class=\"actions\">"
            f"<input type=\"hidden\" name=\"days\" value=\"{escape(days_raw)}\">"
            f"<input type=\"hidden\" name=\"queue\" value=\"{escape(queue)}\">"
            f"<input type=\"hidden\" name=\"dataset\" value=\"{escape(dataset_choice)}\">"
            f"<input type=\"hidden\" name=\"source\" value=\"{escape(source)}\">"
            f"<input type=\"hidden\" name=\"status\" value=\"{escape(status)}\">"
            f"<input type=\"hidden\" name=\"sort\" value=\"{escape(sort_by)}\">"
            f"<input type=\"hidden\" name=\"min_score\" value=\"{escape(str(min_score_raw))}\">"
            f"<label>Re-score Batch<br><select name=\"rescore_limit\">{rescore_options}</select></label>"
            "<button type=\"submit\">Batch Re-score Jobs</button>"
            "</form>"
            "<p class=\"muted\">Batch re-score updates the active dashboard copy for every job in the current filtered view. Durable backing DB writes remain protected for imported sources.</p>"
            "</div>"
            "</div>"
            "<div class=\"card\">"
            "<form method=\"post\" action=\"/jobs/bulk-update\">"
            f"<input type=\"hidden\" name=\"days\" value=\"{escape(days_raw)}\">"
            f"<input type=\"hidden\" name=\"queue\" value=\"{escape(queue)}\">"
            f"<input type=\"hidden\" name=\"dataset\" value=\"{escape(dataset_choice)}\">"
            f"<input type=\"hidden\" name=\"source\" value=\"{escape(source)}\">"
            f"<input type=\"hidden\" name=\"status\" value=\"{escape(status)}\">"
            f"<input type=\"hidden\" name=\"sort\" value=\"{escape(sort_by)}\">"
            f"<input type=\"hidden\" name=\"min_score\" value=\"{escape(str(min_score_raw))}\">"
            f"<input type=\"hidden\" name=\"rescore_limit\" value=\"{escape(rescore_limit_raw)}\">"
            "<div class=\"actions\">"
            "<label>Bulk Status<br><select name=\"bulk_status\">"
            + "".join(f"<option value=\"{escape(item)}\">{escape(item.replace('_', ' ').title())}</option>" for item in PIPELINE_STATUSES)
            + "</select></label>"
            "<button type=\"submit\">Update Selected Jobs</button>"
            "</div>"
            "<div class=\"table-wrap\">"
            "<table><thead><tr><th>Select</th><th>Role</th><th>Location</th><th>Score</th><th>Grade</th><th>Label</th><th>Fetched</th><th>Employer</th><th>Signals</th><th>Status</th><th>Source</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
            "</div>"
            "</form>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Jobs", body)]

    def _job_page(start_response, key: str, dataset_choice: str):
        all_jobs = _dashboard_db_call("list_jobs_for_board", limit=4000)
        hidden_workday_keys = _workday_legacy_duplicate_keys(all_jobs)
        if key in hidden_workday_keys:
            canonical_key = _canonical_workday_key_for_hidden(key, all_jobs)
            if canonical_key:
                return _redirect(start_response, f"/job?key={quote(canonical_key, safe='')}&dataset={quote(dataset_choice, safe='')}")
        job = _dashboard_db_call("get_job", key)
        if job is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Job not found."]

        _mark_job_viewed(key)
        if not str(job.get("viewed_at") or "").strip():
            job = dict(job)
            job["viewed_at"] = datetime.now(timezone.utc).isoformat()
        job, evaluation = _persist_evaluation(job)

        flags = _flags()
        resumes = _dashboard_db_call("list_generated_resumes", job["key"])
        skill_pills = "".join(f"<span class=\"pill\">{escape(skill)}</span>" for skill in (evaluation.matched_strong + evaluation.matched_moderate)[:16])
        reasons = "".join(f"<li>{escape(reason)}</li>" for reason in evaluation.reasons)
        dimensions_html = "".join(
            "<tr>"
            f"<td>{escape(dim.name.replace('_', ' ').title())}</td>"
            f"<td>{int(dim.weight * 100)}%</td>"
            f"<td>{dim.score}</td>"
            f"<td>{round(dim.weighted_points, 1)}</td>"
            f"<td>{escape(dim.reason)}</td>"
            "</tr>"
            for dim in evaluation.dimensions
        )
        resume_block = (
            "".join(
                f"<li><a href=\"{_packet_link(item)}\">Prompt packet #{item['id']}</a> <span class=\"muted\">{escape(item['created_at'])}</span></li>"
                for item in resumes
            ) or "<li>No generated prompt packet yet.</li>"
        )
        skill_match_html = skill_pills or "<span class=\"muted\">No overlapping skills found from the stored text.</span>"
        structured = _job_structured(job)
        structured_rows = []
        posted_value = str(job.get("posted") or "").strip()
        first_seen_value = str(job.get("first_seen") or "").strip()
        effective_posted = posted_value or first_seen_value
        if effective_posted:
            structured_rows.append(
                f"<tr><td>Posted Date</td><td>{escape(effective_posted)}</td></tr>"
            )
        if first_seen_value:
            structured_rows.append(
                f"<tr><td>Extracted / First Seen</td><td>{escape(first_seen_value)}</td></tr>"
            )
        if not posted_value and first_seen_value:
            structured_rows.append(
                "<tr><td>Date Source</td><td>Posted date missing; using extraction timestamp fallback.</td></tr>"
            )
        provenance_value = _display_provenance(job.get("provenance"))
        collected_via, enriched_by = _provenance_labels(job.get("provenance"))
        if collected_via:
            structured_rows.append(
                f"<tr><td>Collected Via</td><td>{escape(collected_via)}</td></tr>"
            )
        if enriched_by:
            structured_rows.append(
                f"<tr><td>Enriched By</td><td>{escape(enriched_by)}</td></tr>"
            )
        if provenance_value:
            structured_rows.append(
                f"<tr><td>Record Origin</td><td>{escape(provenance_value)}</td></tr>"
            )
        for field, label in (
            ("remote_mode", "Remote Mode"),
            ("salary_min", "Salary Min"),
            ("salary_max", "Salary Max"),
            ("salary_currency", "Salary Currency"),
            ("salary_period", "Salary Period"),
            ("years_experience_min", "Experience Min"),
            ("years_experience_max", "Experience Max"),
            ("visa_sponsorship", "Visa Sponsorship"),
            ("security_clearance", "Security Clearance"),
            ("citizenship_requirement", "Citizenship Requirement"),
            ("employment_type", "Employment Type"),
            ("resume_match_score", "Resume/JD Match"),
            ("resume_match_cap", "Resume/JD Cap"),
            ("resume_match_cap_reason", "Resume/JD Cap Reason"),
            ("company_priority_delta", "Company Priority Delta"),
            ("company_priority_reason", "Company Priority Reason"),
            ("label_threshold_yes", "Yes Threshold"),
            ("label_threshold_maybe", "Maybe Threshold"),
            ("feedback_score_delta", "Feedback Score Delta"),
            ("feedback_reasons", "Feedback Reasons"),
        ):
            if field not in structured:
                continue
            value = structured[field]
            if isinstance(value, bool):
                display = "Yes" if value else "No"
            elif isinstance(value, list):
                display = ", ".join(str(item) for item in value) if value else ""
            elif isinstance(value, int) and field.startswith("salary_"):
                display = f"${value:,}"
            else:
                display = str(value)
            structured_rows.append(f"<tr><td>{escape(label)}</td><td>{escape(display)}</td></tr>")
        structured_rows_html = "".join(structured_rows) or "<tr><td colspan=\"2\">No structured fields extracted yet.</td></tr>"
        repost_html = (
            f"<span class=\"pill\">Likely repost of <a href=\"/job?key={quote(job.get('repost_of_key') or '', safe='')}&dataset={quote(dataset_choice, safe='')}\">{escape(job.get('repost_of_key') or '')}</a></span>"
            if int(job.get("is_repost") or 0) and (job.get("repost_of_key") or "")
            else "<span class=\"pill\">Not marked as a repost</span>"
        )

        generate_form = ""
        if flags.get("resume_generation", True):
            generate_form = (
                "<form method=\"post\" action=\"/job/generate-resume\">"
                f"<input type=\"hidden\" name=\"key\" value=\"{escape(job['key'])}\">"
                "<button type=\"submit\">Generate Prompt Packet</button>"
                "</form>"
            )
        resume_action_html = generate_form or "<p class=\"muted\">Resume generation is disabled in the feature switchboard.</p>"
        current_status = job.get("pipeline_status") or "new"
        status_options = "".join(
            f"<option value=\"{escape(status)}\"{' selected' if status == current_status else ''}>{escape(status.replace('_', ' ').title())}</option>"
            for status in PIPELINE_STATUSES
        )

        body = (
            "<div class=\"grid\">"
            "<div>"
            "<div class=\"card\">"
            f"<h1>{escape(job['title'])}</h1>"
            f"<p class=\"muted\">{escape(job['company'])} | {escape(job['location'])} | {escape(job['source'])}</p>"
            f"<p><span class=\"score {escape(job['label'])}\">Score {job['score']}</span> | Grade <strong>{escape(job.get('grade') or evaluation.grade)}</strong> | <strong>{escape(job['label']).upper()}</strong></p>"
            f"<p><span class=\"pill\">Employer Quality {int(job.get('employer_quality_score') or 0)}</span> {repost_html}</p>"
            f"<p>{escape(job.get('fit_summary') or evaluation.fit_summary)}</p>"
            f"<ul>{reasons}</ul>"
            f"<p><a href=\"{escape(job['url'])}\" target=\"_blank\" rel=\"noreferrer\">Open original posting</a></p>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Weighted Evaluation</h2>"
            "<table><thead><tr><th>Dimension</th><th>Weight</th><th>Score</th><th>Points</th><th>Reason</th></tr></thead>"
            f"<tbody>{dimensions_html}</tbody></table>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Stored Job Description</h2>"
            f"<pre>{escape(job.get('description') or 'No job description stored yet for this job.')}</pre>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Structured Extraction</h2>"
            "<table><thead><tr><th>Field</th><th>Value</th></tr></thead>"
            f"<tbody>{structured_rows_html}</tbody></table>"
            "</div>"
            "</div>"
            "<div>"
            "<div class=\"card\">"
            "<h2>Skill Match</h2>"
            f"<div>{skill_match_html}</div>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Inventory Signals</h2>"
            f"<p>{_structured_signal_pills(job)}</p>"
            f"<p class=\"muted\">{escape(job.get('employer_quality_reason') or '')}</p>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Prompt Packet Actions</h2>"
            f"{resume_action_html}"
            "<h3>Generated Prompt Packets</h3>"
            f"<ul>{resume_block}</ul>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Pipeline Tracker</h2>"
            "<form method=\"post\" action=\"/job/pipeline\">"
            f"<input type=\"hidden\" name=\"key\" value=\"{escape(job['key'])}\">"
            f"<label>Status<br><select name=\"pipeline_status\">{status_options}</select></label>"
            f"<label>Follow Up Date<br><input type=\"text\" name=\"follow_up_date\" value=\"{escape(job.get('follow_up_date') or '')}\" placeholder=\"YYYY-MM-DD\"></label>"
            f"<label>Notes<br><textarea name=\"pipeline_notes\">{escape(job.get('pipeline_notes') or '')}</textarea></label>"
            "<div class=\"actions\"><button type=\"submit\">Save Pipeline Status</button></div>"
            "</form>"
            "<p class=\"muted\">Statuses like Applied, Shortlisted, Interview, Rejected, and Archived also feed the lightweight feedback reranker.</p>"
            f"<p class=\"muted\">Last updated: {escape(job.get('pipeline_updated_at') or 'never')}</p>"
            "</div>"
            "</div>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout(job["title"], body)]

    def _manual_page(start_response, values: dict[str, str] | None = None, message: str = ""):
        flags = _flags()
        if not flags.get("manual_jd", True):
            body = "<div class=\"card\"><h1>Paste JD</h1><p class=\"muted\">Manual JD scoring is disabled in the feature switchboard.</p></div>"
            start_response("200 OK", _html_headers())
            return [_layout("Paste JD", body)]

        values = values or {}
        body = (
            "<div class=\"card\">"
            "<h1>Paste an External Job Description</h1>"
            "<p class=\"muted\">Use this for roles that did not come from the scanner. The job is scored, saved, and can then generate a resume draft.</p>"
            f"<p>{escape(message)}</p>"
            "<form method=\"post\" action=\"/manual-jd\">"
            "<div class=\"split\">"
            f"<label>Company<br><input type=\"text\" name=\"company\" value=\"{escape(values.get('company', ''))}\"></label>"
            f"<label>Job Title<br><input type=\"text\" name=\"title\" value=\"{escape(values.get('title', ''))}\" required></label>"
            "</div>"
            "<div class=\"split\">"
            f"<label>Location<br><input type=\"text\" name=\"location\" value=\"{escape(values.get('location', ''))}\"></label>"
            f"<label>Job URL<br><input type=\"url\" name=\"url\" value=\"{escape(values.get('url', ''))}\" placeholder=\"https://...\"></label>"
            "</div>"
            f"<label>Job Description<br><textarea name=\"description\" required>{escape(values.get('description', ''))}</textarea></label>"
            "<div class=\"actions\"><button type=\"submit\">Score and Save Job</button></div>"
            "</form>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Paste JD", body)]

    def _settings_page(start_response):
        flags = _sync_runtime_feature_flags()
        llm_configured = bool(cfg.llm_scoring.enabled and cfg.llm_scoring.api_key)
        llm_runtime_active = bool(EVAL_CFG.llm_scoring.enabled)
        llm_status = (
            "<div class=\"card\">"
            "<h2>LLM Scoring Status</h2>"
            f"<p class=\"muted\">Provider: <strong>{escape(cfg.llm_scoring.provider)}</strong> | "
            f"Model: <strong>{escape(cfg.llm_scoring.model)}</strong></p>"
            f"<p class=\"muted\">Config enabled: <strong>{'yes' if cfg.llm_scoring.enabled else 'no'}</strong> | "
            f"API key loaded: <strong>{'yes' if bool(cfg.llm_scoring.api_key) else 'no'}</strong> | "
            f"Feature flag checked: <strong>{'yes' if flags.get('llm_scoring', False) else 'no'}</strong></p>"
            f"<p class=\"muted\">Runtime active: <strong>{'yes' if llm_runtime_active else 'no'}</strong>. "
            "Runtime active must be yes before Gemini reviews can be written.</p>"
            "</div>"
        )
        items = []
        for name, enabled in flags.items():
            checked = " checked" if enabled else ""
            label = name.replace("_", " ").title()
            detail = ""
            if name == "llm_scoring":
                detail = (
                    " <span class=\"muted\">(requires API key/config; unchecked means no LLM calls)</span>"
                    if llm_configured
                    else " <span class=\"muted\">(not active until app starts with LLM_SCORING_ENABLED=true and GEMINI_API_KEY)</span>"
                )
            items.append(
                f"<label><input type=\"checkbox\" name=\"{escape(name)}\" value=\"1\"{checked}> {escape(label)}{detail}</label>"
            )
        body = (
            "<div class=\"card\">"
            "<h1>Feature Switchboard</h1>"
            "<p class=\"muted\">This is the single toggle panel for the useful features you asked for. Scanner modes stay intact; these switches decide which layers stay active.</p>"
            "<form method=\"post\" action=\"/settings\">"
            "<div class=\"split\">"
            f"{''.join(f'<div>{item}</div>' for item in items)}"
            "</div>"
            "<div class=\"actions\"><button type=\"submit\">Save Feature Settings</button></div>"
            "</form>"
            "</div>"
            f"{llm_status}"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Feature Switchboard", body)]

    def _boards_page(start_response, query: dict[str, list[str]]):
        status = ((query.get("status") or ["all"])[-1] or "all").strip().lower()
        platform = ((query.get("platform") or ["all"])[-1] or "all").strip().lower()
        stats = _dashboard_db_call("get_board_stats")
        boards = _dashboard_db_call(
            "list_boards",
            limit=500,
            status="" if status == "all" else status,
            platform="" if platform == "all" else platform,
        )
        all_boards = _dashboard_db_call("list_boards", limit=5000)
        platforms = sorted({(board.get("platform") or "").strip().lower() for board in all_boards if (board.get("platform") or "").strip()})
        platform_counts: dict[str, int] = {}
        for board in all_boards:
            name = (board.get("platform") or "").strip().lower()
            if not name:
                continue
            platform_counts[name] = platform_counts.get(name, 0) + 1
        rows = []
        for board in boards:
            url = escape(board.get("url") or "")
            rows.append(
                "<tr>"
                f"<td>{escape(board.get('platform') or '')}</td>"
                f"<td>{escape(board.get('company') or '')}</td>"
                f"<td>{escape(board.get('status') or '')}</td>"
                f"<td>{int(board.get('job_count') or 0)}</td>"
                f"<td>{escape(board.get('last_checked') or '')}</td>"
                f"<td>{int(board.get('fail_count') or 0)}</td>"
                f"<td>{escape(board.get('fail_reason') or '')}</td>"
                f"<td><a href=\"{url}\" target=\"_blank\" rel=\"noreferrer\">Open board</a></td>"
                "</tr>"
            )
        rows_html = "".join(rows) if rows else "<tr><td colspan=\"8\">No boards match the current filters.</td></tr>"
        status_options = "".join(
            f"<option value=\"{name}\"{' selected' if status == name else ''}>{label}</option>"
            for name, label in (("all", "All statuses"), ("active", "Active"), ("degraded", "Degraded"), ("broken", "Broken"), ("dead", "Dead"))
        )
        platform_options = "".join(
            f"<option value=\"{escape(name)}\"{' selected' if platform == name else ''}>{escape(name.title())}</option>"
            for name in ["all", *platforms]
        )
        platform_pills = "".join(
            f"<span class=\"pill\">{escape(name.title())}: {count}</span>"
            for name, count in sorted(platform_counts.items())
        ) or "<span class=\"muted\">No boards recorded yet.</span>"
        body = (
            "<div class=\"card\">"
            "<h1>Board Health</h1>"
            "<p class=\"muted\">Track which ATS boards are healthy, which ones are degrading, and which ones are effectively dead before they waste scan time.</p>"
            "<div class=\"stats\">"
            f"<div class=\"stat\"><strong>{stats['total']}</strong><span>Total boards</span></div>"
            f"<div class=\"stat\"><strong>{stats['active']}</strong><span>Active</span></div>"
            f"<div class=\"stat\"><strong>{stats['degraded']}</strong><span>Degraded</span></div>"
            f"<div class=\"stat\"><strong>{stats['broken']}</strong><span>Broken</span></div>"
            f"<div class=\"stat\"><strong>{stats['dead']}</strong><span>Dead</span></div>"
            "</div>"
            f"<div>{platform_pills}</div>"
            "<form method=\"get\" action=\"/boards\" class=\"actions\" data-autosubmit=\"true\">"
            f"<label>Status<br><select name=\"status\">{status_options}</select></label>"
            f"<label>Platform<br><select name=\"platform\">{platform_options}</select></label>"
            "<button type=\"submit\">Apply Filters</button>"
            "</form>"
            "</div>"
            "<div class=\"card\">"
            "<p class=\"muted\">Showing the worst/recent 500 boards for the current filters; summary counts above still include every tracked board.</p>"
            "<div class=\"table-wrap\">"
            "<table><thead><tr><th>Platform</th><th>Company</th><th>Status</th><th>Jobs</th><th>Last Checked</th><th>Failures</th><th>Reason</th><th>URL</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
            "</div>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Board Health", body)]

    def _health_page(start_response, query: dict[str, list[str]]):
        entity_type = ((query.get("type") or ["all"])[-1] or "all").strip().lower()
        mode = ((query.get("mode") or ["all"])[-1] or "all").strip().lower()
        status = ((query.get("status") or ["all"])[-1] or "all").strip().lower()
        health_rows = _dashboard_db_call(
            "get_source_health",
            entity_type="" if entity_type == "all" else entity_type,
            mode="" if mode == "all" else mode,
            status="" if status == "all" else status,
            limit=300,
        )
        run_rows = _dashboard_db_call(
            "list_source_runs",
            limit=100,
            entity_type="" if entity_type == "all" else entity_type,
            mode="" if mode == "all" else mode,
            status="" if status in {"all", "healthy", "degraded", "broken"} else status,
        )
        summary = _dashboard_db_call("get_health_summary")
        type_options = "".join(
            f"<option value=\"{name}\"{' selected' if entity_type == name else ''}>{label}</option>"
            for name, label in (("all", "All"), ("main", "Main Sources"), ("board", "Boards"))
        )
        mode_options = "".join(
            f"<option value=\"{name}\"{' selected' if mode == name else ''}>{label}</option>"
            for name, label in (("all", "All modes"), ("main", "Main"), ("boards", "Boards"))
        )
        status_options = "".join(
            f"<option value=\"{name}\"{' selected' if status == name else ''}>{label}</option>"
            for name, label in (
                ("all", "All states"),
                ("healthy", "Healthy"),
                ("degraded", "Degraded"),
                ("broken", "Broken"),
                ("success", "Last run success"),
                ("empty", "Last run empty"),
                ("skipped", "Last run skipped"),
                ("error", "Last run error"),
            )
        )
        health_table = []
        for row in health_rows:
            health_table.append(
                "<tr>"
                f"<td>{escape(row['source_key'])}</td>"
                f"<td>{escape(row['entity_type'])}</td>"
                f"<td>{escape(row.get('platform') or '')}</td>"
                f"<td>{escape(row.get('company') or '')}</td>"
                f"<td>{escape(row['health'])}</td>"
                f"<td>{escape(row['latest_status'])}</td>"
                f"<td>{escape(row.get('basis_status') or row['latest_status'])}</td>"
                f"<td>{row['latest_fetched_count']}</td>"
                f"<td>{row['latest_matched_count']}</td>"
                f"<td>{row['latest_new_count']}</td>"
                f"<td>{int(round(row['latest_jd_coverage']))}%</td>"
                f"<td>{row['failure_streak']}</td>"
                f"<td>{escape(row.get('last_success_at') or '')}</td>"
                f"<td>{escape(row.get('last_error') or '')}</td>"
                "</tr>"
            )
        health_rows_html = "".join(health_table) if health_table else "<tr><td colspan=\"14\">No health rows match the current filters.</td></tr>"
        run_table = []
        for row in run_rows:
            run_table.append(
                "<tr>"
                f"<td>{escape(row['finished_at'])}</td>"
                f"<td>{escape(row['source_key'])}</td>"
                f"<td>{escape(row['entity_type'])}</td>"
                f"<td>{escape(row['status'])}</td>"
                f"<td>{int(row['fetched_count'])}</td>"
                f"<td>{int(row['matched_count'])}</td>"
                f"<td>{int(row['new_count'])}</td>"
                f"<td>{int(row['latency_ms'])}</td>"
                f"<td>{int(round(float(row['jd_coverage'] or 0.0)))}%</td>"
                f"<td>{escape(row.get('error_text') or '')}</td>"
                "</tr>"
            )
        run_rows_html = "".join(run_table) if run_table else "<tr><td colspan=\"10\">No run history yet.</td></tr>"
        body = (
            "<div class=\"card\">"
            "<h1>Source Health</h1>"
            "<p class=\"muted\">Track both main sources and board runs to spot quality regressions, empty returns, and failing adapters quickly.</p>"
            "<div class=\"stats\">"
            f"<div class=\"stat\"><strong>{summary['total']}</strong><span>Total tracked</span></div>"
            f"<div class=\"stat\"><strong>{summary['healthy']}</strong><span>Healthy</span></div>"
            f"<div class=\"stat\"><strong>{summary['degraded']}</strong><span>Degraded</span></div>"
            f"<div class=\"stat\"><strong>{summary['broken']}</strong><span>Broken</span></div>"
            "</div>"
            f"<p class=\"muted\">Recent hard errors in the last 24 hours: <strong>{summary['failures_24h']}</strong> | Empty runs: <strong>{summary.get('empty_24h', 0)}</strong> | Intentional skips: <strong>{summary.get('skipped_24h', 0)}</strong></p>"
            "<form method=\"get\" action=\"/health\" class=\"actions\" data-autosubmit=\"true\">"
            f"<label>Type<br><select name=\"type\">{type_options}</select></label>"
            f"<label>Mode<br><select name=\"mode\">{mode_options}</select></label>"
            f"<label>Status<br><select name=\"status\">{status_options}</select></label>"
            "<button type=\"submit\">Apply Filters</button>"
            "</form>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Latest Health By Source</h2>"
            "<p class=\"muted\">Showing the highest-priority 300 health rows; use filters to narrow by type, mode, or status.</p>"
            "<div class=\"table-wrap\">"
            "<table><thead><tr><th>Key</th><th>Type</th><th>Platform</th><th>Company</th><th>Health</th><th>Last Status</th><th>Health Basis</th><th>Fetched</th><th>Matched</th><th>New</th><th>JD</th><th>Streak</th><th>Last Success</th><th>Last Error</th></tr></thead>"
            f"<tbody>{health_rows_html}</tbody></table>"
            "</div>"
            "</div>"
            "<div class=\"card\">"
            "<h2>Recent Run History</h2>"
            "<p class=\"muted\">Showing the latest 100 runs for the current filters.</p>"
            "<div class=\"table-wrap\">"
            "<table><thead><tr><th>Finished</th><th>Key</th><th>Type</th><th>Status</th><th>Fetched</th><th>Matched</th><th>New</th><th>Latency ms</th><th>JD</th><th>Error</th></tr></thead>"
            f"<tbody>{run_rows_html}</tbody></table>"
            "</div>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Health", body)]

    def _resume_page(start_response, resume_id: int):
        resume = _dashboard_db_call("get_generated_resume", resume_id)
        if resume is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Resume packet not found."]
        if (resume.get("format") or "").strip().lower() == "prompt_packet":
            return _redirect(start_response, f"/packet?id={resume_id}")
        body = (
            "<div class=\"card\">"
            f"<h1>Resume Packet #{resume['id']}</h1>"
            f"<p class=\"muted\">{escape(resume['job_title'])} | {escape(resume['company'])} | {escape(resume['created_at'])}</p>"
            f"<pre>{escape(resume['content'])}</pre>"
            "</div>"
        )
        start_response("200 OK", _html_headers())
        return [_layout("Resume Packet", body)]

    def _packet_page(start_response, resume_id: int):
        resume = _dashboard_db_call("get_generated_resume", resume_id)
        if resume is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Prompt packet not found."]
        artifacts = _dashboard_db_call("list_generated_artifacts", resume_id)
        artifact_cards: list[str] = []
        for artifact in artifacts:
            artifact_id = int(artifact["id"])
            pre_id = f"artifact-{artifact_id}"
            artifact_cards.append(
                "<div class=\"card\">"
                f"<h2>{escape(artifact.get('name') or 'Artifact')}</h2>"
                f"<div class=\"artifact-meta\"><span class=\"pill\">{escape(artifact.get('filename') or '')}</span><span class=\"pill\">{escape((artifact.get('format') or 'text').upper())}</span></div>"
                "<div class=\"artifact-actions\">"
                f"<button type=\"button\" onclick=\"copyText('{pre_id}')\">Copy</button>"
                f"<a class=\"button-link\" href=\"/artifact?id={artifact_id}\">Download</a>"
                "</div>"
                f"<pre id=\"{pre_id}\">{escape(artifact.get('content') or '')}</pre>"
                "</div>"
            )
        if not artifact_cards:
            artifact_cards.append(
                "<div class=\"card\"><p class=\"muted\">No separate artifacts were saved for this packet.</p>"
                f"<pre>{escape(resume.get('content') or '')}</pre></div>"
            )
        bundle_pre_id = f"packet-bundle-{resume_id}"
        body = (
            "<div class=\"card\">"
            f"<h1>Prompt Packet #{resume['id']}</h1>"
            f"<p class=\"muted\">{escape(resume['job_title'])} | {escape(resume['company'])} | {escape(resume['created_at'])}</p>"
            "<p class=\"muted\">Use the separate files below for prompt, JD markdown, generated resume TeX, and cover letter.</p>"
            "<div class=\"artifact-actions\">"
            f"<button type=\"button\" onclick=\"copyText('{bundle_pre_id}')\">Copy Full Packet</button>"
            "</div>"
            f"<pre id=\"{bundle_pre_id}\">{escape(resume['content'])}</pre>"
            "</div>"
            + "".join(artifact_cards)
        )
        start_response("200 OK", _html_headers())
        return [_layout("Prompt Packet", body)]

    def _artifact_download(start_response, artifact_id: int):
        artifact = _dashboard_db_call("get_generated_artifact", artifact_id)
        if artifact is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Artifact not found."]
        filename = (artifact.get("filename") or f"artifact-{artifact_id}.txt").replace("\"", "")
        start_response(
            "200 OK",
            [
                ("Content-Type", _artifact_content_type(str(artifact.get("format") or ""))),
                ("Content-Disposition", f'attachment; filename="{filename}"'),
                ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
            ],
        )
        return [str(artifact.get("content") or "").encode("utf-8")]

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/") or "/"
        method = environ.get("REQUEST_METHOD", "GET").upper()
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        if method == "GET":
            dataset_choice = ((query.get("dataset") or [current_dataset_preference])[-1] or current_dataset_preference).strip().lower()
            source_signature = _dashboard_source_signature(dataset_choice)
            if (
                dataset_choice != current_dataset_preference
                or not Path(active_dashboard_db_path).exists()
                or source_signature != active_dashboard_source_signature
            ):
                _replace_dashboard_db(dataset_choice)
            _maybe_pull_repo()

        def _redirect_home(form: dict[str, str | list[str]] | None = None, *, message: str = ""):
            form = form or {}
            days = form.get("days", str(RECENT_JOB_DAYS))
            queue = form.get("queue", "active")
            source = form.get("source", "all")
            status = form.get("status", "all")
            sort_by = form.get("sort", "newest")
            min_score = form.get("min_score", "0")
            dataset = form.get("dataset", "merged")
            rescore_limit = form.get("rescore_limit", "500")
            query = (
                f"/?days={quote(str(days), safe='')}"
                f"&queue={quote(str(queue), safe='')}"
                f"&dataset={quote(str(dataset), safe='')}"
                f"&source={quote(str(source), safe='')}"
                f"&status={quote(str(status), safe='')}"
                f"&sort={quote(str(sort_by), safe='')}"
                f"&min_score={quote(str(min_score), safe='')}"
                f"&rescore_limit={quote(str(rescore_limit), safe='')}"
            )
            if message:
                query += f"&message={quote(message, safe='')}"
            return _redirect(
                start_response,
                query,
            )

        if path == "/" and method == "GET":
            return _jobs_page(start_response, query)

        if path == "/job" and method == "GET":
            key = (query.get("key") or [""])[-1]
            dataset_choice = ((query.get("dataset") or [current_dataset_preference])[-1] or current_dataset_preference).strip().lower()
            return _job_page(start_response, key, dataset_choice)

        if path == "/jobs/re-evaluate" and method == "POST":
            form = _read_post(environ)
            dataset_choice = (form.get("dataset", current_dataset_preference) or current_dataset_preference).strip().lower()
            _replace_dashboard_db(dataset_choice)
            rescore_limit_raw = form.get("rescore_limit", "500") or "500"
            if rescore_limit_raw == "all":
                rescore_limit = None
            else:
                try:
                    rescore_limit = max(int(rescore_limit_raw), 1)
                except ValueError:
                    rescore_limit = 500
            jobs = _filtered_jobs_snapshot(form, limit=rescore_limit)
            processed = _batch_refresh(jobs, active_only=True)
            return _redirect_home(form, message=f"Re-scored {processed} job(s) from the current filtered view.")

        if path == "/github-sync" and method == "POST":
            form = _read_post(environ)
            ok, message = _sync_github_archives()
            if not ok:
                log.info(message)
            return _redirect_home(form, message=message)

        if path == "/scan" and method == "POST":
            form = _read_post(environ)
            mode = (form.get("scan_mode", "all") or "all").strip().lower()
            if mode not in {"main", "boards", "broad_boards", "broad_all", "all"}:
                mode = "all"
            if mode == "all" and form.get("confirm_full_sweep") != "1":
                return _redirect_home(
                    form,
                    message="Full board sweep not started. Check the confirmation box if you really want a full local sweep.",
                )
            if mode == "broad_all" and form.get("confirm_broad_sweep") != "1":
                return _redirect_home(
                    form,
                    message="Full broad non-Workday sweep not started. Check the confirmation box if you really want to scan the full broad lane.",
                )
            started = _start_scan(mode)
            if not started:
                current = _scan_snapshot()
                _set_scan_state(
                    message=f"A scan is already running ({current.get('mode') or 'unknown'}). Wait for it to finish before starting another one."
                )
            return _redirect_home(form)

        if path == "/jobs/bulk-update" and method == "POST":
            try:
                size = int(environ.get("CONTENT_LENGTH") or "0")
            except ValueError:
                size = 0
            raw = environ["wsgi.input"].read(size).decode("utf-8") if size > 0 else ""
            parsed = parse_qs(raw, keep_blank_values=True)
            keys = parsed.get("job_key") or []
            bulk_status = ((parsed.get("bulk_status") or ["new"])[-1] or "new").strip()
            for key in keys:
                _dashboard_db_call("update_pipeline", key=key, pipeline_status=bulk_status)
            return _redirect_home(
                {
                    "days": (parsed.get("days") or [str(RECENT_JOB_DAYS)])[-1],
                    "queue": (parsed.get("queue") or ["active"])[-1],
                    "dataset": (parsed.get("dataset") or ["merged"])[-1],
                    "source": (parsed.get("source") or ["all"])[-1],
                    "status": (parsed.get("status") or ["all"])[-1],
                    "sort": (parsed.get("sort") or ["newest"])[-1],
                    "min_score": (parsed.get("min_score") or ["0"])[-1],
                    "rescore_limit": (parsed.get("rescore_limit") or ["500"])[-1],
                }
            )

        if path == "/job/generate-resume" and method == "POST":
            form = _read_post(environ)
            key = form.get("key", "")
            job = _dashboard_db_call("get_job", key)
            if job is None:
                start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
                return [b"Job not found."]
            flags = _flags()
            if not flags.get("resume_generation", True):
                return _redirect(start_response, f"/job?key={quote(key, safe='')}")
            thresholds = _label_thresholds()
            evaluation = evaluate_job(
                job["title"],
                job.get("description", ""),
                company=job["company"],
                location=job["location"],
                source=job.get("source", ""),
                require_us_location=cfg.filter.require_us_location,
                yes_threshold=thresholds.yes,
                maybe_threshold=thresholds.maybe,
            )
            packet = generate_resume_packet(job, evaluation)
            resume_id = _dashboard_db_call(
                "save_generated_resume",
                job_key=job["key"],
                job_title=job["title"],
                company=job["company"],
                content=str(packet["bundle_markdown"]),
                format="prompt_packet",
            )
            for artifact in packet.get("artifacts", []):
                if not isinstance(artifact, dict):
                    continue
                _dashboard_db_call(
                    "save_generated_artifact",
                    resume_id=resume_id,
                    name=str(artifact.get("name") or "Artifact"),
                    filename=str(artifact.get("filename") or ""),
                    format=str(artifact.get("format") or "text"),
                    content=str(artifact.get("content") or ""),
                )
            return _redirect(start_response, f"/packet?id={resume_id}")

        if path == "/job/pipeline" and method == "POST":
            form = _read_post(environ)
            key = form.get("key", "")
            _dashboard_db_call(
                "update_pipeline",
                key=key,
                pipeline_status=form.get("pipeline_status", "new").strip() or "new",
                pipeline_notes=form.get("pipeline_notes", "").strip(),
                follow_up_date=form.get("follow_up_date", "").strip(),
            )
            return _redirect(start_response, f"/job?key={quote(key, safe='')}")

        if path == "/manual-jd" and method == "GET":
            return _manual_page(start_response)

        if path == "/manual-jd" and method == "POST":
            form = _read_post(environ)
            title = form.get("title", "").strip()
            description = form.get("description", "").strip()
            if not title or not description:
                return _manual_page(start_response, form, "Job title and description are required.")
            company = form.get("company", "").strip() or "Manual Entry"
            location = form.get("location", "").strip() or "Unknown Location"
            url = form.get("url", "").strip() or f"manual://{_slug(company)}"
            thresholds = _label_thresholds()
            evaluation = evaluate_job(
                title,
                description,
                company=company,
                location=location,
                source="manual",
                require_us_location=cfg.filter.require_us_location,
                yes_threshold=thresholds.yes,
                maybe_threshold=thresholds.maybe,
            )
            digest = hashlib.sha1(f"{company.lower()}|{title.lower()}|{description[:240]}".encode("utf-8")).hexdigest()[:12]
            manual_key = f"manual:{_slug(company)}:{_slug(title)}:{digest}"
            _dashboard_db_call(
                "create_manual_job",
                key=manual_key,
                company=company,
                title=title,
                location=location,
                url=url,
                description=description,
                score=evaluation.score,
                label=evaluation.label,
                grade=evaluation.grade,
                evaluation_json=evaluation.to_json(),
                fit_summary=evaluation.fit_summary,
            )
            return _redirect(start_response, f"/job?key={quote(manual_key, safe='')}")

        if path == "/settings" and method == "GET":
            return _settings_page(start_response)

        if path == "/boards" and method == "GET":
            return _boards_page(start_response, query)

        if path == "/health" and method == "GET":
            return _health_page(start_response, query)

        if path == "/settings" and method == "POST":
            form = _read_post(environ)
            for name in defaults:
                _set_feature_flag_everywhere(name, form.get(name) == "1")
            _sync_runtime_feature_flags()
            return _redirect(start_response, "/settings")

        if path == "/resume" and method == "GET":
            raw_id = (query.get("id") or ["0"])[-1]
            try:
                resume_id = int(raw_id)
            except ValueError:
                resume_id = 0
            return _resume_page(start_response, resume_id)

        if path == "/packet" and method == "GET":
            raw_id = (query.get("id") or ["0"])[-1]
            try:
                resume_id = int(raw_id)
            except ValueError:
                resume_id = 0
            return _packet_page(start_response, resume_id)

        if path == "/artifact" and method == "GET":
            raw_id = (query.get("id") or ["0"])[-1]
            try:
                artifact_id = int(raw_id)
            except ValueError:
                artifact_id = 0
            return _artifact_download(start_response, artifact_id)

        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found."]

    def _run_startup_github_sync() -> None:
        startup_sync_ok, startup_sync_message = _sync_github_archives()
        if startup_sync_ok:
            log.info(startup_sync_message)
        else:
            log.warning(startup_sync_message)
        try:
            _replace_dashboard_db("merged")
        except Exception as exc:
            log.warning("Dashboard DB refresh after startup sync failed: %s", exc)

    threading.Thread(
        target=_run_startup_github_sync,
        name="job-radar-startup-github-sync",
        daemon=True,
    ).start()

    try:
        with make_server(host, port, app) as httpd:
            print(f"Job Radar web UI running at http://{host}:{port}")
            httpd.serve_forever()
    finally:
        _log_public_db_shutdown_note()
