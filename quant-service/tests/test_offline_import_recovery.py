from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.main import offline_import_recovery_action, offline_minute_import_stale_seconds


class OfflineMinuteImportRecoveryTests(unittest.TestCase):
    def test_terminal_files_are_not_reimported_and_failures_can_resume(self) -> None:
        now = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(offline_import_recovery_action(None, now=now, stale_seconds=900), "create")
        self.assertEqual(offline_import_recovery_action({"status": "completed"}, now=now, stale_seconds=900), "unchanged")
        self.assertEqual(offline_import_recovery_action({"status": "partial"}, now=now, stale_seconds=900), "unchanged")
        self.assertEqual(offline_import_recovery_action({"status": "failed"}, now=now, stale_seconds=900), "resume_failed")

    def test_running_import_has_a_real_stale_boundary(self) -> None:
        now = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(
            offline_import_recovery_action({"status": "running", "started_at": now - timedelta(seconds=899)}, now=now, stale_seconds=900),
            "in_progress",
        )
        self.assertEqual(
            offline_import_recovery_action({"status": "running", "started_at": now - timedelta(seconds=900)}, now=now, stale_seconds=900),
            "resume_stale_running",
        )
        self.assertEqual(
            offline_import_recovery_action({"status": "running", "started_at": None}, now=now, stale_seconds=900),
            "resume_stale_running",
        )

    def test_stale_window_is_bounded_and_sql_uses_a_hash_lock(self) -> None:
        self.assertEqual(offline_minute_import_stale_seconds({"OFFLINE_MINUTE_IMPORT_STALE_SECONDS": "1"}), 60)
        self.assertEqual(offline_minute_import_stale_seconds({"OFFLINE_MINUTE_IMPORT_STALE_SECONDS": "999999"}), 86_400)
        self.assertEqual(offline_minute_import_stale_seconds({"OFFLINE_MINUTE_IMPORT_STALE_SECONDS": "bad"}), 900)
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        window = source[source.index("def import_offline_minute_csv"):source.index("def normalize_tushare_rows")]
        self.assertIn("pg_advisory_xact_lock", window)
        self.assertIn("FOR UPDATE", window)


if __name__ == "__main__":
    unittest.main()
