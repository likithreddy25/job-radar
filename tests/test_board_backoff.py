import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.database import Database


class EmptyBoardBackoffTests(unittest.TestCase):
    def test_empty_board_cooldown_doubles_after_threshold(self) -> None:
        self.assertEqual(Database.empty_board_cooldown_hours(3), 168)
        self.assertEqual(Database.empty_board_cooldown_hours(4), 336)
        self.assertEqual(Database.empty_board_cooldown_hours(5), 672)

    def test_empty_board_cooldown_is_capped_at_thirty_days(self) -> None:
        for fail_count in (6, 10, 50, 10**6):
            self.assertEqual(
                Database.empty_board_cooldown_hours(fail_count),
                Database.MAX_EMPTY_BOARD_COOLDOWN_HOURS,
            )


class SkipBoardBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(tempfile.mkdtemp(), "boards.db")
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.close()

    def _board(
        self,
        board_id: str,
        *,
        status: str = "degraded",
        fail_count: int,
        days_since_check: float,
        reason: str = "0 jobs returned",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        checked = (datetime.now(timezone.utc) - timedelta(days=days_since_check)).isoformat()
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO boards(board_id,platform,company,url,status,last_checked,job_count,"
            "fail_count,fail_reason,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (board_id, "greenhouse", "Example", "https://example.com", status, checked, 0, fail_count, reason, now, now),
        )
        conn.commit()
        conn.close()

    def test_long_empty_board_is_skipped_for_expanded_cooldown(self) -> None:
        self._board("stale-empty", fail_count=10, days_since_check=14)
        self.assertTrue(self.db.should_skip_board("stale-empty"))

    def test_empty_board_is_retried_after_monthly_cap(self) -> None:
        self._board("stale-empty", fail_count=10, days_since_check=31)
        self.assertFalse(self.db.should_skip_board("stale-empty"))

    def test_non_empty_failure_reason_keeps_normal_retry_behavior(self) -> None:
        self._board("http-error", fail_count=10, days_since_check=14, reason="HTTP 500")
        self.assertFalse(self.db.should_skip_board("http-error"))

    def test_active_board_is_never_skipped_by_empty_backoff(self) -> None:
        self._board("active", status="active", fail_count=10, days_since_check=1)
        self.assertFalse(self.db.should_skip_board("active"))


if __name__ == "__main__":
    unittest.main()
