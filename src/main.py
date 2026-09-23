"""Job Radar — main orchestrator and CLI entry point.

Improvements over the original watcher.py:
- Modular source architecture (each source is an independent class)
- SQLite state (no more unboundedly growing JSON files)
- Concurrent main-source fetching via ThreadPoolExecutor
- Per-platform semaphores in boards mode
- HTML emails + optional Slack/Discord webhooks
- Structured logging (replace print statements)
- Rich CLI output with live progress
- Score-based job ranking
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .classifier import is_match, classify
from .company_priority import company_score_adjustment
from .config import Config
from .database import Database
from .evaluation import evaluate_job
from .feedback_scorer import build_feedback_adjustments
from .job_intelligence import to_structured_json
from .notifier import CompositeNotifier, EmailNotifier, SlackNotifier, DiscordNotifier
from .scoring_policy import calibrate_thresholds, label_for_score, resume_fit_cap
from .sources.base import Job, is_us_location, remote_scope_status
from .sources.eightfold import EightfoldSource
from .sources.amazon import AmazonSource
from .sources.goldman import GoldmanSachsSource
from .sources.ibm import IBMSource
from .sources.oracle import OracleSource
from .sources.meta import MetaSource
from .sources.google import GoogleSource
from .sources.apple import AppleSource
from .sources.netflix import NetflixSource
from .sources.stripe import StripeSource
from .sources.linkedin import LinkedInSource
from .sources.google_x import GoogleXSource
from .sources.tiktok_usds import TikTokUSDSSource
from .sources.greenhouse import GreenhouseSource, _board_id as gh_board_id
from .sources.lever import LeverSource, _board_id as lever_board_id
from .sources.smartrecruiters import SmartRecruitersSource, _board_id as sr_board_id
from .sources.workday import WorkdaySource, _board_id as wd_board_id
from .sources.ashby import AshbySource, _board_id as ashby_board_id
from .sources.workable import WorkableSource, _board_id as workable_board_id
from .sources.jobvite import JobviteSource, _board_id as jobvite_board_id
from .sources.icims import ICIMSSource, _board_id as icims_board_id
from .sources.recruitee import RecruiteeSource, _board_id as recruitee_board_id
from .sources.breezyhr import BreezyHRSource, _board_id as breezyhr_board_id
from .sources.teamtailor import TeamtailorSource, _board_id as teamtailor_board_id
from .sources.dover import DoverSource, _board_id as dover_board_id
from .sources.gem import GemSource, _board_id as gem_board_id
from .sources.wellfound import WellfoundSource, _board_id as wellfound_board_id
from .sources.workatastartup import WorkAtAStartupSource, _board_id as workatastartup_board_id
from .sources.generic_html import GenericJobsHTMLBoardSource, board_id as generic_html_board_id
from .webapp import serve_web

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

SUPPORTED_BOARD_PLATFORMS = (
    "greenhouse", "lever", "smartrecruiters", "workday", "ashby", "workable", "jobvite", "icims",
    "recruitee", "breezyhr", "teamtailor", "dover", "gem", "wellfound", "workatastartup",
    "generic_html",
)

log = logging.getLogger(__name__)


def _auto_sync_repo_before_web(*, repo_root: str, branch: str = "main") -> None:
    """Best-effort fast-forward pull before opening the local web UI.

    This keeps the dashboard aligned with the latest GitHub-committed DB state
    without pulling over local uncommitted work.
    """
    git_dir = Path(repo_root) / ".git"
    if not git_dir.exists():
        log.info("Web auto-sync skipped: %s is not a git repository.", repo_root)
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
        log.warning("Web auto-sync skipped: could not inspect git status (%s).", exc)
        return
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "").strip()
        log.warning("Web auto-sync skipped: git status failed%s", f" ({detail})" if detail else ".")
        return
    if status.stdout.strip():
        log.info("Web auto-sync skipped: local repo has uncommitted changes.")
        return
    try:
        pull = subprocess.run(
            ["git", "pull", "--ff-only", "origin", branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        log.warning("Web auto-sync failed before startup: %s", exc)
        return
    output = (pull.stdout or pull.stderr or "").strip()
    if pull.returncode == 0:
        if output:
            log.info("Web auto-sync: %s", output.replace("\n", " | "))
        else:
            log.info("Web auto-sync completed before startup.")
        return
    log.warning("Web auto-sync failed before startup: %s", output or f"git exited with {pull.returncode}")


def _open_database(cfg: Config) -> Database:
    return Database(cfg.database.path)


def _web_port_responding(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _active_git_maintenance_markers(repo_root: str) -> list[str]:
    repo_path = Path(repo_root)
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return []
    markers: list[tuple[str, Path]] = [
        ("index.lock", git_dir / "index.lock"),
        ("HEAD.lock", git_dir / "HEAD.lock"),
        ("rebase-apply", git_dir / "rebase-apply"),
        ("rebase-merge", git_dir / "rebase-merge"),
        ("MERGE_HEAD", git_dir / "MERGE_HEAD"),
        ("CHERRY_PICK_HEAD", git_dir / "CHERRY_PICK_HEAD"),
        ("REVERT_HEAD", git_dir / "REVERT_HEAD"),
        ("BISECT_LOG", git_dir / "BISECT_LOG"),
    ]
    return [name for name, path in markers if path.exists()]


def _public_export_git_maintenance_markers(repo_root: str) -> list[str]:
    root = Path(repo_root)
    public_export = (root / "public-export").resolve()
    if public_export == root.resolve():
        return _active_git_maintenance_markers(str(public_export))
    if not public_export.exists():
        return []
    return _active_git_maintenance_markers(str(public_export))

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    # Quiet noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Board CSV loader
# ---------------------------------------------------------------------------

def load_boards_csv(path: str) -> list[dict]:
    raw = (path or "").strip()
    if not raw:
        raise FileNotFoundError("No boards CSV path specified.")

    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        for base in (Path.cwd(), Path(ROOT_DIR)):
            candidate = base / p
            if candidate.exists():
                p = candidate
                break

    if not p.exists():
        raise FileNotFoundError(f"Boards CSV not found: {path}")

    rows: list[dict] = []
    with open(p, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            company = (r.get("company_name") or r.get("company") or "").strip()
            platform = (r.get("platform") or "").strip().lower()
            url = (r.get("board_url") or r.get("url") or "").strip()
            ok_val = (r.get("ok") or "").strip().lower()
            if ok_val and ok_val not in ("true", "1", "yes"):
                continue
            if not company or not platform or not url:
                continue
            rows.append({"company": company, "platform": platform, "board_url": url.rstrip("/")})

    # Deduplicate on (platform, url)
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for r in rows:
        k = (r["platform"], r["board_url"])
        if k not in seen:
            seen.add(k)
            deduped.append(r)

    return deduped


def _resolve_boards_csv(cfg_path: str) -> str:
    """Try several fallback locations for the boards CSV."""
    candidates = [
        cfg_path,
        os.environ.get("BOARDS_CSV", ""),
        "data/boards/JOB_BOARDS_PURE_WORKING_SUPPORTED_round2.csv",
        "data/boards/JOB_BOARDS_PURE_WORKING_round2.csv",
        "data/boards/JOB_BOARDS_OK_PRODUCTION.csv",
    ]
    for raw in candidates:
        if not raw:
            continue
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            for base in (Path.cwd(), Path(ROOT_DIR)):
                candidate = base / p
                if candidate.exists():
                    return str(candidate)
        elif p.exists():
            return str(p)
    raise FileNotFoundError("Could not locate a boards CSV file. Specify --boards-csv or set BOARDS_CSV.")


def _resolve_priority_csv(csv_path: str) -> str:
    """Resolve the curated priority-company CSV."""
    candidates = [
        csv_path,
        os.environ.get("PRIORITY_CSV", ""),
        "data/boards/PRIORITY_COMPANIES.csv",
    ]
    for raw in candidates:
        if not raw:
            continue
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            for base in (Path.cwd(), Path(ROOT_DIR)):
                candidate = base / p
                if candidate.exists():
                    return str(candidate)
        elif p.exists():
            return str(p)
    raise FileNotFoundError("Could not locate a priority companies CSV file. Specify --priority-csv or set PRIORITY_CSV.")


# ---------------------------------------------------------------------------
# Notifier factory
# ---------------------------------------------------------------------------

def build_notifier(cfg: Config) -> CompositeNotifier:
    notifiers = []

    email = EmailNotifier(
        user=cfg.email.user,
        password=cfg.email.password,
        to=cfg.email.to,
        smtp_host=cfg.email.smtp_host,
        smtp_port=cfg.email.smtp_port,
    )
    if email.is_configured():
        notifiers.append(email)
    else:
        log.warning("Email not configured — no email alerts will be sent.")

    slack = SlackNotifier(cfg.slack.webhook_url)
    if slack.is_configured():
        notifiers.append(slack)

    discord = DiscordNotifier(cfg.discord.webhook_url)
    if discord.is_configured():
        notifiers.append(discord)

    return CompositeNotifier(notifiers)


# ---------------------------------------------------------------------------
# Job age filter — drop stale listings older than MAX_JOB_AGE_DAYS
# ---------------------------------------------------------------------------

MAX_JOB_AGE_DAYS = 30

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d",
]


def _parse_posted(posted: str) -> Optional[datetime]:
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(posted[:26], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _is_too_old(posted: str, max_days: int = MAX_JOB_AGE_DAYS) -> bool:
    """Return True if job was posted more than max_days ago. Unknown dates pass through."""
    dt = _parse_posted(posted)
    if dt is None:
        return False  # can't parse → don't filter out
    return dt < datetime.now(timezone.utc) - timedelta(days=max_days)


def _dedup_jobs(jobs: list[Job]) -> list[Job]:
    """Remove duplicates using the best stable identity available."""
    best: dict[tuple, Job] = {}
    for j in jobs:
        canonical_key = str(getattr(j, "canonical_key", "") or "").strip().lower()
        normalized_url = re.sub(r"[?#].*$", "", (j.url or "").strip().lower()).rstrip("/")
        location = (j.location or "").strip().lower()
        if canonical_key:
            fp = ("canonical", canonical_key)
        elif normalized_url:
            fp = ("url", normalized_url)
        else:
            fp = ("triple", j.company.strip().lower(), j.title.strip().lower(), location)
        if fp not in best or j.score > best[fp].score:
            best[fp] = j
    return list(best.values())


def _passes_initial_location_filter(location: str, *, require_us_location: bool) -> bool:
    if not require_us_location:
        return True
    if is_us_location(location):
        return True
    return remote_scope_status(location) == "unspecified"


def _load_structured_json(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dimension_score(evaluation, name: str) -> int:
    for dim in getattr(evaluation, "dimensions", []) or []:
        if dim.name == name:
            return int(dim.score)
    return 0


def _resume_match_score(evaluation) -> int:
    """Expose a separate resume-vs-JD signal from the richer evaluator."""
    if not getattr(evaluation, "dimensions", None):
        return max(0, min(100, int(getattr(evaluation, "score", 0) or 0)))
    evidence = _dimension_score(evaluation, "evidence_quality")
    overlap = _dimension_score(evaluation, "skill_overlap")
    return max(0, min(100, round(0.6 * evidence + 0.4 * overlap)))


def _relabel(score: int, thresholds) -> str:
    return label_for_score(score, yes_threshold=thresholds.yes, maybe_threshold=thresholds.maybe)


def _grade_for_score(score: int) -> str:
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


# ---------------------------------------------------------------------------
# Main mode: company career pages
# ---------------------------------------------------------------------------

def _build_main_sources(cfg: Config) -> list[object]:
    sources = []
    if cfg.source("microsoft").enabled:
        sources.append(EightfoldSource("microsoft", max_jobs=cfg.source("microsoft").max_jobs))
    if cfg.source("nvidia").enabled:
        sources.append(EightfoldSource("nvidia", max_jobs=cfg.source("nvidia").max_jobs))
    if cfg.source("amazon").enabled:
        sources.append(AmazonSource(max_jobs=cfg.source("amazon").max_jobs))
    if cfg.source("goldman_sachs").enabled:
        sources.append(GoldmanSachsSource(max_jobs=cfg.source("goldman_sachs").max_jobs))
    if cfg.source("ibm").enabled:
        sources.append(IBMSource(max_jobs=cfg.source("ibm").max_jobs))
    if cfg.source("oracle").enabled:
        sources.append(OracleSource(max_jobs=cfg.source("oracle").max_jobs))
    if cfg.source("meta").enabled:
        sources.append(MetaSource(max_jobs=cfg.source("meta").max_jobs))
    if cfg.source("google").enabled:
        sources.append(GoogleSource(max_jobs=cfg.source("google").max_jobs))
    if cfg.source("apple").enabled:
        sources.append(AppleSource(max_jobs=cfg.source("apple").max_jobs))
    if cfg.source("netflix").enabled:
        sources.append(NetflixSource(max_jobs=cfg.source("netflix").max_jobs))
    if cfg.source("stripe").enabled:
        sources.append(StripeSource(max_jobs=cfg.source("stripe").max_jobs))
    if cfg.source("linkedin").enabled:
        sources.append(LinkedInSource(max_jobs=cfg.source("linkedin").max_jobs))
    if cfg.source("google_x").enabled:
        sources.append(GoogleXSource(max_jobs=cfg.source("google_x").max_jobs))
    if cfg.source("tiktok_usds").enabled:
        sources.append(TikTokUSDSSource(max_jobs=cfg.source("tiktok_usds").max_jobs))
    return sources


def run_main(cfg: Config, db: Database, notifier: CompositeNotifier, *, dry_run: bool, no_notify: bool, test_notify: bool) -> None:
    """Fetch jobs from configured company sources concurrently."""
    if not db.get_feature_flags({"scanner_main": cfg.features.scanner_main})["scanner_main"]:
        log.warning("scanner_main feature is disabled. Skipping main mode run.")
        return

    timeout = cfg.http_timeout

    # Build source list based on config
    sources = _build_main_sources(cfg)

    if not sources:
        log.warning("No sources enabled.")
        return

    log.info("Running MAIN mode — %d source(s)", len(sources))

    # Fetch all sources concurrently
    all_jobs: list[Job] = []
    errors: list[str] = []
    run_records: list[dict] = []

    with ThreadPoolExecutor(max_workers=min(len(sources), 12)) as pool:
        future_map = {
            pool.submit(_fetch_source_metrics, src, db, timeout): src.name
            for src in sources
        }
        for fut in as_completed(future_map):
            src_name = future_map[fut]
            try:
                jobs, record = fut.result()
                run_records.append(record)
                all_jobs.extend(jobs)
                log.info("%-20s fetched %d jobs", src_name, record["fetched_count"])
            except Exception as exc:
                err = f"{src_name}: {type(exc).__name__}: {exc}"
                errors.append(err)
                now = datetime.now(timezone.utc).isoformat()
                run_records.append(
                    {
                        "source_key": src_name,
                        "entity_type": "main",
                        "mode": "main",
                        "platform": src_name,
                        "company": src_name.replace("_", " ").title(),
                        "url": "",
                        "status": "error",
                        "started_at": now,
                        "finished_at": now,
                        "latency_ms": 0,
                        "fetched_count": 0,
                        "jd_coverage": 0.0,
                        "error_text": err,
                        "job_keys": [],
                    }
                )
                log.error("Source failed — %s", err)

    summary = _dispatch_results(
        all_jobs=all_jobs, errors=errors, db=db, notifier=notifier,
        mode="main", dry_run=dry_run, no_notify=no_notify, test_notify=test_notify,
        cfg=cfg, notify_yes_only=False,
    )
    if not dry_run:
        notifications_enabled = db.get_feature_flags({"notifications": cfg.features.notifications})["notifications"]
        alert_lines = _record_run_history(db=db, run_records=run_records, summary=summary, mode="main")
        _maybe_send_failure_alerts(
            notifier=notifier,
            alert_lines=alert_lines,
            notifications_enabled=notifications_enabled,
            no_notify=no_notify,
            test_notify=test_notify,
            mode="main",
        )


def run_priority(
    cfg: Config,
    db: Database,
    notifier: CompositeNotifier,
    *,
    priority_csv: str,
    timeout: int,
    workers: int,
    dry_run: bool,
    no_notify: bool,
    test_notify: bool,
    notify_yes_only: bool = False,
) -> None:
    """Run the frequent/high-priority company set: main sources + curated boards."""
    all_jobs: list[Job] = []
    errors: list[str] = []
    run_records: list[dict] = []

    if db.get_feature_flags({"scanner_main": cfg.features.scanner_main})["scanner_main"]:
        main_sources = _build_main_sources(cfg)
        if main_sources:
            log.info("Running PRIORITY mode main sources — %d source(s)", len(main_sources))
            with ThreadPoolExecutor(max_workers=min(len(main_sources), 12)) as pool:
                future_map = {
                    pool.submit(_fetch_source_metrics, src, db, cfg.http_timeout): src.name
                    for src in main_sources
                }
                for fut in as_completed(future_map):
                    src_name = future_map[fut]
                    try:
                        jobs, record = fut.result()
                        run_records.append(record)
                        all_jobs.extend(jobs)
                        log.info("%-20s fetched %d jobs", src_name, record["fetched_count"])
                    except Exception as exc:
                        err = f"{src_name}: {type(exc).__name__}: {exc}"
                        errors.append(err)
                        now = datetime.now(timezone.utc).isoformat()
                        run_records.append(
                            {
                                "source_key": src_name,
                                "entity_type": "main",
                                "mode": "priority",
                                "platform": src_name,
                                "company": src_name.replace("_", " ").title(),
                                "url": "",
                                "status": "error",
                                "started_at": now,
                                "finished_at": now,
                                "latency_ms": 0,
                                "fetched_count": 0,
                                "jd_coverage": 0.0,
                                "error_text": err,
                                "job_keys": [],
                            }
                        )
                        log.error("Priority source failed — %s", err)

    if not db.get_feature_flags({"scanner_boards": cfg.features.scanner_boards})["scanner_boards"]:
        log.warning("scanner_boards feature is disabled. Skipping curated priority boards.")
    else:
        boards = load_boards_csv(priority_csv)
        boards = [b for b in boards if b.get("platform") in SUPPORTED_BOARD_PLATFORMS]
        if boards:
            platform_counts = Counter(b["platform"] for b in boards)
            log.info(
                "Running PRIORITY mode boards — %d curated rows | %s",
                len(boards),
                " ".join(f"{p}={c}" for p, c in sorted(platform_counts.items())),
            )
            board_jobs, board_errors, board_records = _process_boards_batch(
                boards,
                db,
                timeout,
                workers,
                cfg.boards.rescan_cooldown_hours,
                progress_callback=None,
            )
            all_jobs.extend(board_jobs)
            errors.extend(board_errors)
            run_records.extend(board_records)
            log.info(
                "Priority boards done — %d jobs fetched, %d errors",
                len(board_jobs),
                len(board_errors),
            )
        else:
            log.warning("No supported curated priority boards found in CSV.")

    if not run_records:
        log.warning("No priority sources were run.")
        return

    summary = _dispatch_results(
        all_jobs=all_jobs,
        errors=errors,
        db=db,
        notifier=notifier,
        mode="priority",
        dry_run=dry_run,
        no_notify=no_notify,
        test_notify=test_notify,
        cfg=cfg,
        notify_yes_only=notify_yes_only,
    )
    if not dry_run:
        notifications_enabled = db.get_feature_flags({"notifications": cfg.features.notifications})["notifications"]
        alert_lines = _record_run_history(db=db, run_records=run_records, summary=summary, mode="priority")
        _maybe_send_failure_alerts(
            notifier=notifier,
            alert_lines=alert_lines,
            notifications_enabled=notifications_enabled,
            no_notify=no_notify,
            test_notify=test_notify,
            mode="priority",
        )


def _fetch_source(source, db: Database, timeout: int) -> list[Job]:
    seen_keys = db.get_seen_keys(source.name)
    return source.fetch(seen_keys=seen_keys, timeout=timeout)


def _jd_coverage(jobs: list[Job]) -> float:
    if not jobs:
        return 0.0
    covered = sum(1 for job in jobs if (job.description or "").strip())
    return round((100.0 * covered) / len(jobs), 1)


def _rehydrate_descriptions_from_db(jobs: list[Job], db: Database) -> list[Job]:
    for job in jobs:
        current = (job.description or "").strip()
        if len(current) >= 120:
            continue
        stored = db.get_job(job.key)
        if not stored:
            continue
        stored_desc = (stored.get("description") or "").strip()
        if len(stored_desc) > len(current):
            job.description = stored_desc
    return jobs


def _fetch_source_metrics(source, db: Database, timeout: int) -> tuple[list[Job], dict]:
    started_at = datetime.now(timezone.utc)
    jobs = _fetch_source(source, db, timeout)
    jobs = _rehydrate_descriptions_from_db(jobs, db)
    finished_at = datetime.now(timezone.utc)
    status = "success" if jobs else "empty"
    record = {
        "source_key": source.name,
        "entity_type": "main",
        "mode": "main",
        "platform": source.name,
        "company": source.name.replace("_", " ").title(),
        "url": "",
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "latency_ms": int((finished_at - started_at).total_seconds() * 1000),
        "fetched_count": len(jobs),
        "jd_coverage": _jd_coverage(jobs),
        "error_text": "0 jobs returned" if status == "empty" else "",
        "job_keys": [job.key for job in jobs],
    }
    return jobs, record


# ---------------------------------------------------------------------------
# Boards mode: ATS board sweep
# ---------------------------------------------------------------------------

_BOARD_SEMAPHORES: dict[str, threading.Semaphore] = {
    "greenhouse": threading.Semaphore(8),
    "lever": threading.Semaphore(8),
    "smartrecruiters": threading.Semaphore(6),
    "workday": threading.Semaphore(4),
    "ashby": threading.Semaphore(6),
    "workable": threading.Semaphore(6),
    "jobvite": threading.Semaphore(6),
    "icims": threading.Semaphore(6),
    "recruitee": threading.Semaphore(6),
    "breezyhr": threading.Semaphore(6),
    "teamtailor": threading.Semaphore(6),
    "dover": threading.Semaphore(6),
    "gem": threading.Semaphore(6),
    "wellfound": threading.Semaphore(4),
    "workatastartup": threading.Semaphore(4),
    "generic_html": threading.Semaphore(4),
}


def run_boards(
    cfg: Config,
    db: Database,
    notifier: CompositeNotifier,
    *,
    boards_csv: str,
    batch_size: int,
    timeout: int,
    workers: int,
    dry_run: bool,
    no_notify: bool,
    test_notify: bool,
    notify_yes_only: bool = False,
    run_until_wrap: bool = False,
    max_iterations: int = 2000,
    export_dead_csv: str = "",
    show_live_progress: bool = True,
    cursor_key: str = "boards_main",
 ) -> dict:
    if not db.get_feature_flags({"scanner_boards": cfg.features.scanner_boards})["scanner_boards"]:
        log.warning("scanner_boards feature is disabled. Skipping boards mode run.")
        return

    if export_dead_csv and dry_run:
        db.export_dead_boards_csv(export_dead_csv)
        log.info("Exported dead boards report to %s without running a sweep batch.", export_dead_csv)
        return {}

    boards = load_boards_csv(boards_csv)
    boards = [b for b in boards if b.get("platform") in SUPPORTED_BOARD_PLATFORMS]

    if not boards:
        log.error("No supported boards found in CSV.")
        return

    platform_counts = Counter(b["platform"] for b in boards)
    log.info(
        "Boards CSV: %d supported rows | %s",
        len(boards),
        " ".join(f"{p}={c}" for p, c in sorted(platform_counts.items())),
    )

    n = len(boards)

    def run_one_batch() -> int:
        cursor = db.get_cursor(cursor_key)
        start = cursor % n
        end = min(start + max(batch_size, 1), n)
        batch = boards[start:end]
        progress_start = min(start, n)
        progress_end = min(end, n)
        progress_start_human = min(start + 1, n) if batch else progress_start
        progress_end_pct = (progress_end / n * 100.0) if n else 0.0
        use_live_progress = show_live_progress and not os.environ.get("GITHUB_ACTIONS")
        progress_log_step = max(10, max(batch_size, 1) // 5) if batch else 10

        log.info("Processing batch [%d:%d] of %d boards", start, end, n)
        if batch:
            if use_live_progress:
                current_pct = ((progress_start_human / n) * 100.0) if n else 0.0
                filled = int((current_pct / 100.0) * 24)
                bar = "#" * filled + "-" * (24 - filled)
                sys.stdout.write(
                    f"\rBoards sweep: {progress_start_human}/{n} [{bar}] {current_pct:5.1f}% | batch {start}:{end}"
                )
                sys.stdout.flush()

        completed_in_batch = 0
        last_logged_progress = start
        last_logged_at = time.time()

        def _log_board_progress() -> None:
            nonlocal completed_in_batch, last_logged_progress, last_logged_at
            completed_in_batch += 1
            current = min(start + completed_in_batch, n)
            current_pct = (current / n * 100.0) if n else 0.0
            if use_live_progress:
                filled = int((current_pct / 100.0) * 24)
                bar = "#" * filled + "-" * (24 - filled)
                sys.stdout.write(
                    f"\rBoards sweep: {current}/{n} [{bar}] {current_pct:5.1f}% | batch {start}:{end}"
                )
                sys.stdout.flush()
            else:
                now = time.time()
                should_log = (
                    current >= progress_end
                    or (current - last_logged_progress) >= progress_log_step
                    or (now - last_logged_at) >= 30.0
                )
                if should_log:
                    log.info("Sweep progress: %d/%d boards scanned (%.1f%%)", current, n, current_pct)
                    last_logged_progress = current
                    last_logged_at = now

        t0 = time.time()
        all_jobs, errors, run_records = _process_boards_batch(
            batch,
            db,
            timeout,
            workers,
            cfg.boards.rescan_cooldown_hours,
            progress_callback=_log_board_progress,
        )
        elapsed = time.time() - t0
        if use_live_progress:
            sys.stdout.write("\n")
            sys.stdout.flush()
        log.info("Batch done in %.1fs — %d jobs fetched, %d errors", elapsed, len(all_jobs), len(errors))

        if not use_live_progress:
            log.info("Sweep progress: scanned through %d/%d boards (%.1f%%)", progress_end, n, progress_end_pct)

        for err in errors:
            log.warning("Board error: %s", err)

        summary = _dispatch_results(
            all_jobs=all_jobs, errors=errors, db=db, notifier=notifier,
            mode="boards", dry_run=dry_run, no_notify=no_notify, test_notify=test_notify,
            cfg=cfg, notify_yes_only=notify_yes_only,
        )
        if not dry_run:
            notifications_enabled = db.get_feature_flags({"notifications": cfg.features.notifications})["notifications"]
            alert_lines = _record_run_history(db=db, run_records=run_records, summary=summary, mode="boards")
            _maybe_send_failure_alerts(
                notifier=notifier,
                alert_lines=alert_lines,
                notifications_enabled=notifications_enabled,
                no_notify=no_notify,
                test_notify=test_notify,
                mode="boards",
            )

        new_cursor = end if end < n else 0
        if not dry_run:
            db.set_cursor(cursor_key, new_cursor)
            db.set_cursor_value(f"{cursor_key}_updated_at", datetime.now(timezone.utc).isoformat())
            if export_dead_csv:
                db.export_dead_boards_csv(export_dead_csv)

        return new_cursor

    if run_until_wrap:
        for it in range(1, max_iterations + 1):
            cur = run_one_batch()
            log.info("[%d] cursor=%d", it, cur)
            if cur == 0:
                log.info("Full sweep complete (cursor wrapped to 0).")
                break
    else:
        run_one_batch()


def _process_boards_batch(
    batch: list[dict],
    db: Database,
    timeout: int,
    workers: int,
    board_rescan_cooldown_hours: int,
    progress_callback=None,
) -> tuple[list[Job], list[str], list[dict]]:
    all_jobs: list[Job] = []
    errors: list[str] = []
    run_records: list[dict] = []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(_process_one_board, b, db, timeout, board_rescan_cooldown_hours)
            for b in batch
        ]
        for fut in as_completed(futures):
            try:
                jobs, err, record = fut.result()
                run_records.append(record)
                if err:
                    errors.append(err)
                all_jobs.extend(jobs)
            except Exception as exc:
                errors.append(f"Board thread error: {type(exc).__name__}: {exc}")
                now = datetime.now(timezone.utc).isoformat()
                run_records.append(
                    {
                        "source_key": "unknown-board",
                        "entity_type": "board",
                        "mode": "boards",
                        "platform": "",
                        "company": "",
                        "url": "",
                        "status": "error",
                        "started_at": now,
                        "finished_at": now,
                        "latency_ms": 0,
                        "fetched_count": 0,
                        "jd_coverage": 0.0,
                        "error_text": f"Board thread error: {type(exc).__name__}: {exc}",
                        "job_keys": [],
                    }
                )
            finally:
                if progress_callback is not None:
                    progress_callback()

    return all_jobs, errors, run_records


def _board_source_for(b: dict) -> Optional[object]:
    platform = b["platform"]
    company = b["company"]
    url = b["board_url"]
    if platform == "greenhouse":
        return GreenhouseSource(company, url)
    if platform == "lever":
        return LeverSource(company, url)
    if platform == "smartrecruiters":
        return SmartRecruitersSource(company, url)
    if platform == "workday":
        return WorkdaySource(company, url)
    if platform == "ashby":
        return AshbySource(company, url)
    if platform == "workable":
        return WorkableSource(company, url)
    if platform == "jobvite":
        return JobviteSource(company, url)
    if platform == "icims":
        return ICIMSSource(company, url)
    if platform == "recruitee":
        return RecruiteeSource(company, url)
    if platform == "breezyhr":
        return BreezyHRSource(company, url)
    if platform == "teamtailor":
        return TeamtailorSource(company, url)
    if platform == "dover":
        return DoverSource(company, url)
    if platform == "gem":
        return GemSource(company, url)
    if platform == "wellfound":
        return WellfoundSource(company, url)
    if platform == "workatastartup":
        return WorkAtAStartupSource(company, url)
    if platform == "generic_html":
        return GenericJobsHTMLBoardSource(company, url)
    return None


def _get_board_id(b: dict) -> str:
    platform = b["platform"]
    url = b["board_url"]
    if platform == "greenhouse":
        return gh_board_id(url)
    if platform == "lever":
        return lever_board_id(url)
    if platform == "smartrecruiters":
        return sr_board_id(url)
    if platform == "workday":
        return wd_board_id(url)
    if platform == "ashby":
        return ashby_board_id(url)
    if platform == "workable":
        return workable_board_id(url)
    if platform == "jobvite":
        return jobvite_board_id(url)
    if platform == "icims":
        return icims_board_id(url)
    if platform == "recruitee":
        return recruitee_board_id(url)
    if platform == "breezyhr":
        return breezyhr_board_id(url)
    if platform == "teamtailor":
        return teamtailor_board_id(url)
    if platform == "dover":
        return dover_board_id(url)
    if platform == "gem":
        return gem_board_id(url)
    if platform == "wellfound":
        return wellfound_board_id(url)
    if platform == "workatastartup":
        return workatastartup_board_id(url)
    if platform == "generic_html":
        return generic_html_board_id("generic_html", url)
    return f"{platform}:"


def _process_one_board(
    b: dict,
    db: Database,
    timeout: int,
    board_rescan_cooldown_hours: int,
) -> tuple[list[Job], Optional[str], dict]:
    import requests

    platform = b["platform"]
    company = b["company"]
    url = b["board_url"]
    board_id = _get_board_id(b)
    started_at = datetime.now(timezone.utc)

    def _record(*, status: str, jobs: list[Job], error_text: str = "") -> dict:
        finished_at = datetime.now(timezone.utc)
        return {
            "source_key": board_id,
            "entity_type": "board",
            "mode": "boards",
            "platform": platform,
            "company": company,
            "url": url,
            "status": status,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "latency_ms": int((finished_at - started_at).total_seconds() * 1000),
            "fetched_count": len(jobs),
            "jd_coverage": _jd_coverage(jobs),
            "error_text": error_text or ("0 jobs returned" if status == "empty" else ""),
            "job_keys": [job.key for job in jobs],
        }

    if db.is_board_dead(board_id):
        return [], None, _record(status="error", jobs=[], error_text="Board marked dead")
    if db.should_skip_board(board_id):
        log.debug("Skipping board with repeated recent failures: %s", board_id)
        return [], None, _record(status="skipped", jobs=[], error_text="Skipped after repeated recent failures")
    if db.was_board_checked_recently(board_id, cooldown_hours=board_rescan_cooldown_hours):
        log.debug("Skipping recently checked board: %s", board_id)
        return [], None, _record(
            status="skipped",
            jobs=[],
            error_text=f"Skipped because board was checked within the last {board_rescan_cooldown_hours}h",
        )

    source = _board_source_for(b)
    if source is None:
        return [], None, _record(status="error", jobs=[], error_text="Unsupported board platform")

    sem = _BOARD_SEMAPHORES.get(platform)
    t0 = time.time()

    try:
        with sem:
            jobs = source.fetch(seen_keys=set(), timeout=timeout)
        jobs = _rehydrate_descriptions_from_db(jobs, db)

        elapsed = time.time() - t0

        is_first_run = not db.is_board_bootstrapped(board_id)
        db.upsert_board(
            board_id=board_id,
            platform=platform,
            company=company,
            url=url,
            status="active" if jobs else "degraded",
            job_count=len(jobs),
            fail_reason="" if jobs else "0 jobs returned",
        )
        if is_first_run:
            log.debug("Board bootstrapped: %s (%d jobs discovered)", board_id, len(jobs))
        return jobs, None, _record(status="success" if jobs else "empty", jobs=jobs)

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            db.upsert_board(board_id=board_id, platform=platform, company=company, url=url,
                            status="dead", fail_reason=f"HTTP {status}")
            error_text = f"DEAD {board_id}: HTTP {status}"
            return [], error_text, _record(status="error", jobs=[], error_text=error_text)
        error_text = f"{board_id}: HTTPError {status}: {exc}"
        db.upsert_board(
            board_id=board_id,
            platform=platform,
            company=company,
            url=url,
            status="broken",
            fail_reason=error_text[:500],
        )
        return [], error_text, _record(status="error", jobs=[], error_text=error_text)

    except Exception as exc:
        error_text = f"{board_id}: {type(exc).__name__}: {exc}"
        db.upsert_board(
            board_id=board_id,
            platform=platform,
            company=company,
            url=url,
            status="broken",
            fail_reason=error_text[:500],
        )
        return [], error_text, _record(status="error", jobs=[], error_text=error_text)


# ---------------------------------------------------------------------------
# Shared result dispatcher
# ---------------------------------------------------------------------------

def _dispatch_results(
    *,
    all_jobs: list[Job],
    errors: list[str],
    db: Database,
    notifier: CompositeNotifier,
    mode: str,
    dry_run: bool,
    no_notify: bool,
    test_notify: bool,
    cfg: Config,
    notify_yes_only: bool,
) -> None:
    notifications_enabled = db.get_feature_flags({"notifications": cfg.features.notifications})["notifications"]

    # Filter by classification + location
    matched = [
        j for j in all_jobs
        if j.label in ("yes", "maybe") and (
            _passes_initial_location_filter(j.location, require_us_location=cfg.filter.require_us_location)
        )
    ]

    # Age filter — drop listings older than MAX_JOB_AGE_DAYS
    before_age = len(matched)
    stale_jobs = [j for j in matched if _is_too_old(j.posted)]
    matched = [j for j in matched if not _is_too_old(j.posted)]
    dropped = before_age - len(matched)
    if dropped:
        log.info("Age filter: dropped %d stale job(s) older than %d days", dropped, MAX_JOB_AGE_DAYS)

    # Deduplication — prefer canonical/job URL identity, then fall back to company/title/location
    before_dedup = len(matched)
    matched = _dedup_jobs(matched)
    dupes = before_dedup - len(matched)
    if dupes:
        log.info("Dedup: removed %d duplicate(s)", dupes)

    get_feedback_jobs = getattr(db, "get_feedback_jobs", None)
    feedback_rows = get_feedback_jobs() if callable(get_feedback_jobs) else []
    thresholds = calibrate_thresholds(feedback_rows)
    if thresholds.calibrated:
        log.info(
            "Outcome-calibrated thresholds: yes>=%d maybe>=%d (%d positive / %d negative feedback rows).",
            thresholds.yes,
            thresholds.maybe,
            thresholds.positive_count,
            thresholds.negative_count,
        )

    # Structured evaluation + company priority/exclusion
    filtered_matched: list[Job] = []
    company_excluded = 0
    company_boosted = 0
    resume_capped = 0
    for j in matched:
        evaluation = evaluate_job(
            j.title,
            j.description,
            company=j.company,
            location=j.location,
            source=j.source,
            require_us_location=cfg.filter.require_us_location,
            yes_threshold=thresholds.yes,
            maybe_threshold=thresholds.maybe,
        )
        setattr(j, "_evaluation_result", evaluation)
        j.score = evaluation.score
        j.label = evaluation.label
        intelligence = db.inspect_job(
            key=j.key,
            source=j.source,
            company=j.company,
            title=j.title,
            location=j.location,
            url=j.url,
            description=j.description,
        )
        merged_structured = dict(intelligence.get("structured") or {})
        merged_structured.update(_load_structured_json(getattr(j, "structured_json", "")))
        resume_match_score = _resume_match_score(evaluation)
        company_delta, company_reason = company_score_adjustment(j.company)
        if company_delta == -999:
            company_excluded += 1
            continue
        if company_delta > 0:
            company_boosted += 1
            j.score = min(100, j.score + company_delta)
            j.label = _relabel(j.score, thresholds)
        resume_cap, resume_cap_reason = resume_fit_cap(resume_match_score)
        if j.score > resume_cap:
            resume_capped += 1
            j.score = resume_cap
            j.label = _relabel(j.score, thresholds)
        merged_structured["resume_match_score"] = resume_match_score
        merged_structured["resume_match_cap"] = resume_cap
        merged_structured["resume_match_cap_reason"] = resume_cap_reason
        merged_structured["company_priority_delta"] = company_delta
        merged_structured["company_priority_reason"] = company_reason
        merged_structured["label_threshold_yes"] = thresholds.yes
        merged_structured["label_threshold_maybe"] = thresholds.maybe
        j.structured_json = to_structured_json(merged_structured)
        j.is_repost = intelligence["is_repost"]
        j.repost_of_key = intelligence["repost_of_key"]
        j.canonical_key = intelligence["canonical_key"]
        j.employer_quality_score = intelligence["employer_quality_score"]
        j.employer_quality_reason = intelligence["employer_quality_reason"]
        filtered_matched.append(j)

    matched = filtered_matched
    if company_excluded:
        log.info("Company filter: excluded %d job(s) by employer rule.", company_excluded)
    if company_boosted:
        log.info("Company priority: boosted %d job(s).", company_boosted)
    if resume_capped:
        log.info("Resume fit cap: capped %d job(s) after boosts.", resume_capped)

    feedback_adjustments = build_feedback_adjustments(matched, feedback_rows)
    feedback_adjusted = 0
    feedback_capped = 0
    for j in matched:
        adjustment = feedback_adjustments.get(j.key)
        if adjustment is None:
            continue
        if not adjustment.delta and not adjustment.reasons:
            continue
        if adjustment.delta:
            feedback_adjusted += 1
            j.score = max(0, min(100, j.score + adjustment.delta))
        merged_structured = _load_structured_json(getattr(j, "structured_json", ""))
        resume_cap = int(merged_structured.get("resume_match_cap") or 100)
        if j.score > resume_cap:
            feedback_capped += 1
            j.score = resume_cap
        j.label = _relabel(j.score, thresholds)
        merged_structured["feedback_score_delta"] = adjustment.delta
        merged_structured["feedback_reasons"] = adjustment.reasons
        j.structured_json = to_structured_json(merged_structured)

    if feedback_adjusted:
        log.info("Feedback rescoring: adjusted %d job(s) using recorded user actions.", feedback_adjusted)
    if feedback_capped:
        log.info("Resume fit cap: held back %d job(s) after feedback adjustments.", feedback_capped)

    yes_jobs = sorted([j for j in matched if j.label == "yes"], key=lambda j: j.score, reverse=True)
    maybe_jobs = sorted([j for j in matched if j.label == "maybe"], key=lambda j: j.score, reverse=True)

    log.info("Matched: %d yes, %d maybe", len(yes_jobs), len(maybe_jobs))

    # Summarise source errors for the email footer
    source_errors = [e for e in errors if e]

    if test_notify:
        sample_yes = yes_jobs[:2]
        sample_maybe = maybe_jobs[:1] if (sample_yes or not notify_yes_only) else []
        if not (sample_yes or sample_maybe):
            log.error("No matching jobs found for test notification.")
            sys.exit(1)
        if not no_notify and notifications_enabled:
            errs = notifier.notify(sample_yes, sample_maybe, subject_prefix=f"[TEST Job Radar]", mode=mode, source_errors=source_errors)
            for e in errs:
                log.error("Notifier error: %s", e)
        else:
            log.info("[TEST] Would notify: %d yes + %d maybe", len(sample_yes), len(sample_maybe))
        return {
            "matched_keys": {j.key for j in matched},
            "yes_keys": {j.key for j in yes_jobs},
            "maybe_keys": {j.key for j in maybe_jobs},
            "new_keys": {j.key for j in sample_yes + sample_maybe},
            "stale_keys": {j.key for j in stale_jobs},
            "source_errors": source_errors,
        }

    # Determine which jobs are new (not yet in DB)
    reposts_suppressed = sum(1 for j in matched if getattr(j, "is_repost", False) and db.is_new_job(j.key))
    new_yes = [j for j in yes_jobs if db.is_new_job(j.key) and not getattr(j, "is_repost", False)]
    new_maybe = [j for j in maybe_jobs if db.is_new_job(j.key) and not getattr(j, "is_repost", False)]
    if reposts_suppressed:
        log.info("Repost filter: suppressed %d likely repost alert(s)", reposts_suppressed)

    log.info("New jobs: %d yes, %d maybe", len(new_yes), len(new_maybe))

    notify_yes = new_yes
    notify_maybe = new_maybe if (notify_yes or not notify_yes_only) else []
    if notify_yes_only and new_maybe and not new_yes:
        log.info("Skipping notification because only MAYBE matches were found.")

    if notify_yes or notify_maybe:
        if no_notify or not notifications_enabled:
            log.info("[no-notify] Would alert: %d yes + %d maybe", len(notify_yes), len(notify_maybe))
        else:
            errs = notifier.notify(notify_yes, notify_maybe, subject_prefix="[Job Radar]", mode=mode, source_errors=source_errors)
            for e in errs:
                log.error("Notifier error: %s", e)
    else:
        log.info("No new matching jobs.")

    # Persist all seen jobs to DB
    if not dry_run:
        for j in matched:
            evaluation = getattr(j, "_evaluation_result", None)
            if evaluation is None:
                evaluation = evaluate_job(
                    j.title,
                    j.description,
                    company=j.company,
                    location=j.location,
                    source=j.source,
                    require_us_location=cfg.filter.require_us_location,
                    yes_threshold=thresholds.yes,
                    maybe_threshold=thresholds.maybe,
                )
            evaluation_payload = evaluation.to_dict()
            evaluation_payload["score"] = int(j.score)
            evaluation_payload["label"] = str(j.label)
            evaluation_payload["grade"] = _grade_for_score(int(j.score))
            evaluation_payload["fit_summary"] = f"Grade {evaluation_payload['grade']} ({int(j.score)}/100). " + " ".join(
                str(reason) for reason in evaluation_payload.get("reasons", [])[:3]
            )
            db.mark_job_seen(
                key=j.key, source=j.source, company=j.company, title=j.title,
                location=j.location, url=j.url, posted=j.posted,
                score=j.score, label=j.label,
                grade=evaluation_payload["grade"],
                evaluation_json=json.dumps(evaluation_payload, ensure_ascii=True, sort_keys=True),
                description=j.description,
                fit_summary=evaluation_payload["fit_summary"],
                canonical_key=getattr(j, "canonical_key", ""),
                structured_json=getattr(j, "structured_json", ""),
                is_repost=bool(getattr(j, "is_repost", False)),
                repost_of_key=getattr(j, "repost_of_key", ""),
                employer_quality_score=int(getattr(j, "employer_quality_score", 50) or 50),
                employer_quality_reason=getattr(j, "employer_quality_reason", ""),
            )
        log.debug("Saved %d jobs to database.", len(matched))

        # Auto-expiry: clean up jobs not seen in 14 days to keep the DB lean.
        db.expire_old_jobs(days=14)
    return {
        "matched_keys": {j.key for j in matched},
        "yes_keys": {j.key for j in yes_jobs},
        "maybe_keys": {j.key for j in maybe_jobs},
        "new_keys": {j.key for j in new_yes + new_maybe},
        "stale_keys": {j.key for j in stale_jobs},
        "source_errors": source_errors,
    }


def _record_run_history(*, db: Database, run_records: list[dict], summary: dict, mode: str) -> list[str]:
    matched_keys = set(summary.get("matched_keys") or set())
    yes_keys = set(summary.get("yes_keys") or set())
    maybe_keys = set(summary.get("maybe_keys") or set())
    new_keys = set(summary.get("new_keys") or set())
    stale_keys = set(summary.get("stale_keys") or set())
    alert_lines: list[str] = []

    for record in run_records:
        job_keys = set(record.get("job_keys") or [])
        record["matched_count"] = len(job_keys & matched_keys)
        record["yes_count"] = len(job_keys & yes_keys)
        record["maybe_count"] = len(job_keys & maybe_keys)
        record["new_count"] = len(job_keys & new_keys)
        record["stale_count"] = len(job_keys & stale_keys)
        previous = db.get_latest_source_run(record["source_key"], record["entity_type"])
        db.record_source_run(
            source_key=record["source_key"],
            entity_type=record["entity_type"],
            mode=mode,
            platform=record.get("platform", ""),
            company=record.get("company", ""),
            url=record.get("url", ""),
            status=record.get("status", "success"),
            started_at=record["started_at"],
            finished_at=record["finished_at"],
            latency_ms=record.get("latency_ms", 0),
            fetched_count=record.get("fetched_count", 0),
            matched_count=record.get("matched_count", 0),
            new_count=record.get("new_count", 0),
            yes_count=record.get("yes_count", 0),
            maybe_count=record.get("maybe_count", 0),
            stale_count=record.get("stale_count", 0),
            jd_coverage=record.get("jd_coverage", 0.0),
            error_text=record.get("error_text", ""),
        )
        if _should_alert_on_run(previous, record):
            alert_lines.append(_format_failure_alert(record))
    return alert_lines


def _should_alert_on_run(previous: Optional[dict], current: dict) -> bool:
    status = current.get("status", "success")
    if status == "success":
        return False
    if previous is None:
        return True
    previous_status = previous.get("status", "success")
    if previous_status == "success":
        return True
    if status == "empty" and int(previous.get("fetched_count") or 0) > 0:
        return True
    return False


def _format_failure_alert(record: dict) -> str:
    label = record.get("company") or record.get("source_key") or "unknown"
    kind = "Board" if record.get("entity_type") == "board" else "Source"
    status = str(record.get("status") or "error").upper()
    error_text = (record.get("error_text") or "").strip()
    details = error_text if error_text else f"fetched={int(record.get('fetched_count') or 0)}"
    return f"{kind} {label} is {status}: {details}"


def _maybe_send_failure_alerts(
    *,
    notifier: CompositeNotifier,
    alert_lines: list[str],
    notifications_enabled: bool,
    no_notify: bool,
    test_notify: bool,
    mode: str,
) -> None:
    if not alert_lines or not notifications_enabled or no_notify or test_notify:
        return
    log.info("Suppressing standalone source alert notifications (%d issue(s)).", len(alert_lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_health_check(cfg: Config, db: Database, notifier: CompositeNotifier) -> None:
    """Send a weekly health-check summary email."""
    from datetime import datetime, timezone
    stats = db.get_stats()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    subject = f"[Job Radar] Weekly Health Check — {ts}"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
          background:#f5f5f5; margin:0; padding:20px; color:#333; }}
  .container {{ max-width:600px; margin:0 auto; background:#fff;
                border-radius:8px; overflow:hidden;
                box-shadow:0 2px 8px rgba(0,0,0,.1); }}
  .header {{ background:#1a1a2e; color:#fff; padding:20px 28px; }}
  .header h1 {{ margin:0; font-size:20px; }}
  .header p  {{ margin:4px 0 0; font-size:13px; color:#aaa; }}
  .body {{ padding:24px 28px; }}
  .stat {{ display:flex; justify-content:space-between; padding:10px 0;
           border-bottom:1px solid #f0f0f0; font-size:14px; }}
  .stat-val {{ font-weight:700; color:#1d4ed8; }}
  .ok {{ color:#16a34a; font-weight:700; }}
  .footer {{ background:#f9f9f9; border-top:1px solid #eee;
             padding:14px 28px; font-size:12px; color:#888; }}
</style></head><body>
<div class="container">
  <div class="header">
    <h1>Job Radar ✅ Weekly Health Check</h1>
    <p>{ts}</p>
  </div>
  <div class="body">
    <p class="ok">Job Radar is running and monitoring 916+ companies for you.</p>
    <div class="stat"><span>Jobs found (last 24 hrs)</span><span class="stat-val">{stats['new_24h']}</span></div>
    <div class="stat"><span>Jobs found (last 7 days)</span><span class="stat-val">{stats['new_7d']}</span></div>
    <div class="stat"><span>Total YES matches in DB</span><span class="stat-val">{stats['yes_count']}</span></div>
    <div class="stat"><span>Total MAYBE matches in DB</span><span class="stat-val">{stats['maybe_count']}</span></div>
    <div class="stat"><span>Total jobs tracked</span><span class="stat-val">{stats['total_jobs']}</span></div>
    <div class="stat"><span>Last job activity</span><span class="stat-val">{stats['last_activity'][:19]}</span></div>
    <div class="stat"><span>ATS boards tracked</span><span class="stat-val">{stats['boards']['total']} ({stats['boards']['active']} active)</span></div>
  </div>
  <div class="footer">Powered by Job Radar — targeting Data Analyst · Data Scientist · Data Engineer</div>
</div></body></html>"""

    text = (
        f"Job Radar Weekly Health Check — {ts}\n\n"
        f"✅ Job Radar is running and monitoring 916+ companies.\n\n"
        f"Jobs found last 24h : {stats['new_24h']}\n"
        f"Jobs found last 7d  : {stats['new_7d']}\n"
        f"Total YES in DB     : {stats['yes_count']}\n"
        f"Total MAYBE in DB   : {stats['maybe_count']}\n"
        f"Total jobs tracked  : {stats['total_jobs']}\n"
        f"Last activity       : {stats['last_activity'][:19]}\n"
        f"ATS boards tracked  : {stats['boards']['total']} ({stats['boards']['active']} active)\n"
    )

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    for n in notifier._notifiers:
        if hasattr(n, "smtp_host"):  # EmailNotifier
            if not n.is_configured():
                log.warning("Email not configured for health check.")
                return
            import smtplib, ssl as _ssl
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = n.user
            msg["To"] = n.to
            msg.attach(MIMEText(text, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
            try:
                import certifi
                ctx = _ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = _ssl.create_default_context()
            if n.smtp_port == 465:
                with smtplib.SMTP_SSL(n.smtp_host, n.smtp_port, context=ctx) as s:
                    s.login(n.user, n.password); s.send_message(msg)
            else:
                with smtplib.SMTP(n.smtp_host, n.smtp_port) as s:
                    s.ehlo(); s.starttls(context=ctx); s.ehlo()
                    s.login(n.user, n.password); s.send_message(msg)
            log.info("Health check email sent to %s", n.to)
            return
    log.warning("No email notifier configured — health check skipped.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job-radar",
        description="Job Radar — aggregate and alert on new engineering jobs.",
    )
    p.add_argument("--config", default="config.yaml", help="Path to YAML config file (default: config.yaml)")
    p.add_argument("--mode", default="main", choices=["main", "boards", "priority", "web"], help="Run mode (default: main)")
    p.add_argument("--dry-run", action="store_true", help="Fetch jobs but do not save state or send notifications.")
    p.add_argument("--no-notify", action="store_true", help="Save state but skip all notifications.")
    p.add_argument("--notify-yes-only", action="store_true", help="Send notifications only when there is at least one YES match; include MAYBE matches only alongside YES alerts.")
    p.add_argument("--test-notify", action="store_true", help="Send a sample notification without updating state.")
    p.add_argument("--health-check", action="store_true", help="Send a weekly health-check summary email and exit.")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging.")
    p.add_argument("--web-host", default="127.0.0.1", help="Web UI host (used with --mode web).")
    p.add_argument("--web-port", type=int, default=8080, help="Web UI port (used with --mode web).")
    p.add_argument("--web-no-git-sync", action="store_true", help="Skip the automatic git pull before starting the web UI.")

    # Boards options
    bg = p.add_argument_group("Boards mode options")
    bg.add_argument("--boards-csv", default="", help="Path to boards CSV file.")
    bg.add_argument("--boards-batch-size", type=int, default=0, help="Boards per run (0 = use config value).")
    bg.add_argument("--boards-timeout", type=int, default=0, help="HTTP timeout for boards (0 = use config value).")
    bg.add_argument("--boards-workers", type=int, default=0, help="Parallel board workers (0 = use config value).")
    bg.add_argument("--boards-run-until-wrap", action="store_true", help="Run batches until cursor wraps (full sweep).")
    bg.add_argument("--boards-max-iterations", type=int, default=2000, help="Safety cap for --boards-run-until-wrap.")
    bg.add_argument("--export-dead-csv", default="", help="Export dead boards to a CSV file.")
    bg.add_argument("--priority-csv", default="", help="Path to curated priority-companies CSV file.")

    return p


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if args.mode == "web":
        if _web_port_responding(args.web_host, args.web_port):
            log.warning("A web server is already responding at http://%s:%d", args.web_host, args.web_port)
            return
        maintenance_markers = _public_export_git_maintenance_markers(str(Path(ROOT_DIR)))
        if maintenance_markers:
            log.warning(
                "Web mode refused to start: public-export has active git maintenance markers (%s). "
                "Finish the git operation or remove the stale lock before opening the dashboard.",
                ", ".join(maintenance_markers),
            )
            return
        if not args.web_no_git_sync:
            _auto_sync_repo_before_web(repo_root=str(Path(ROOT_DIR)), branch="main")

    cfg = Config.load(args.config)

    db = _open_database(cfg)

    notifier = build_notifier(cfg)

    try:
        if args.health_check:
            run_health_check(cfg=cfg, db=db, notifier=notifier)
            return

        if args.mode == "main":
            run_main(
                cfg=cfg, db=db, notifier=notifier,
                dry_run=args.dry_run, no_notify=args.no_notify, test_notify=args.test_notify,
            )
        elif args.mode == "boards":
            # Boards mode — resolve CSV and override config values if CLI flags given
            boards_csv = args.boards_csv or cfg.boards.csv
            try:
                boards_csv = _resolve_boards_csv(boards_csv)
            except FileNotFoundError as exc:
                log.error("%s", exc)
                sys.exit(1)

            batch_size = args.boards_batch_size or cfg.boards.batch_size
            timeout = args.boards_timeout or cfg.boards.timeout
            workers = args.boards_workers or cfg.boards.workers

            run_boards(
                cfg=cfg, db=db, notifier=notifier,
                boards_csv=boards_csv,
                batch_size=batch_size,
                timeout=timeout,
                workers=workers,
                dry_run=args.dry_run,
                no_notify=args.no_notify,
                test_notify=args.test_notify,
                notify_yes_only=args.notify_yes_only,
                run_until_wrap=args.boards_run_until_wrap,
                max_iterations=args.boards_max_iterations,
                export_dead_csv=args.export_dead_csv,
            )
        elif args.mode == "priority":
            try:
                priority_csv = _resolve_priority_csv(args.priority_csv)
            except FileNotFoundError as exc:
                log.error("%s", exc)
                sys.exit(1)

            timeout = args.boards_timeout or cfg.boards.timeout
            workers = args.boards_workers or cfg.boards.workers
            run_priority(
                cfg=cfg,
                db=db,
                notifier=notifier,
                priority_csv=priority_csv,
                timeout=timeout,
                workers=workers,
                dry_run=args.dry_run,
                no_notify=args.no_notify,
                test_notify=args.test_notify,
                notify_yes_only=args.notify_yes_only,
            )
        else:
            serve_web(
                cfg=cfg,
                db=db,
                host=args.web_host,
                port=args.web_port,
                repo_root=str(Path(ROOT_DIR)),
                auto_pull_interval_seconds=300,
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()

