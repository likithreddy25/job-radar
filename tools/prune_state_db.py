"""Keep a GitHub-Actions state DB under GitHub's 100 MB file limit.

Why this exists (2026-09-23): `source_runs` is an append-only per-board/per-source
run log with no retention. On state/gha-boards.db it grew to ~82 MB of the file's
~95 MB (266k rows since May). Once the DB crossed the workflow's 95 MiB safety
threshold, the "Commit state" step started skipping the commit AND running
`git restore` on the DB -- so every board sweep since 2026-09-13 has thrown away its
own results (new jobs, cursor, board health), silently, while the workflow still
shows green.

This script:
  1. deletes source_runs rows older than --keep-days, always keeping the latest row
     per (source_key, entity_type) so backoff / "last checked" logic still works;
  2. checkpoints the WAL and VACUUMs;
  3. prints before/after sizes.

Usage:
    python tools/prune_state_db.py state/gha-boards.db --keep-days 14
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def prune(db_path: Path, keep_days: int) -> None:
    before = db_path.stat().st_size
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_runs'"
        ).fetchone()
        deleted = 0
        if has_table:
            cur = conn.execute(
                """
                DELETE FROM source_runs
                WHERE COALESCE(finished_at, started_at) < ?
                  AND id NOT IN (
                      SELECT MAX(id) FROM source_runs GROUP BY source_key, entity_type
                  )
                """,
                (cutoff,),
            )
            deleted = cur.rowcount
            conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = db_path.stat().st_size
    print(
        f"Pruned {db_path}: deleted {deleted} source_runs rows older than {keep_days}d; "
        f"size {before / 1048576:.1f} MiB -> {after / 1048576:.1f} MiB"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", type=Path)
    ap.add_argument("--keep-days", type=int, default=14)
    args = ap.parse_args()
    if not args.db.exists():
        print(f"{args.db} not found; nothing to prune.")
        return
    prune(args.db, args.keep_days)


if __name__ == "__main__":
    main()
