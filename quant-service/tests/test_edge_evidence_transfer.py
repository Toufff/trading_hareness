from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.edge_evidence_transfer import (
    CHANGE_PAGE_SIZE, CHANGE_REPLAY_WINDOW, TRANSFER_TABLES, edge_evidence_status,
    assess_live_session_acceptance, read_live_session_acceptance, write_live_session_acceptance,
    parse_checkpoint, parse_restricted_export_command, parse_sequence, parse_since,
    read_cursor_payload, read_pull_status, upsert_statement, write_pull_status,
)


class EdgeEvidenceTransferTests(unittest.TestCase):
    def test_live_session_acceptance_requires_fresh_expected_data_loops(self) -> None:
        now = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
        base_health = {"status": "ok", "optional_background_tasks": {"runtime_profile": "intraday_edge"}, "build": {"git_sha": "a1b2c3d"}}
        healthy_status = {
            "session_active": True, "session_reason": "continuous auction", "items": [
                {"key": "fuyao_ths_realtime", "expected_active": True, "state": "healthy", "last_observed_at": now.isoformat(), "age_seconds": 4, "max_age_seconds": 45, "last_error": None},
                {"key": "feishu_alert", "expected_active": True, "state": "ready", "last_observed_at": None, "age_seconds": None, "max_age_seconds": None, "last_error": None},
            ],
        }
        passed = assess_live_session_acceptance(base_health, healthy_status, now=now)
        self.assertEqual(passed["state"], "passed")
        stale = assess_live_session_acceptance(base_health, {**healthy_status, "items": [{**healthy_status["items"][0], "age_seconds": 60}, healthy_status["items"][1]]}, now=now)
        self.assertEqual(stale["state"], "failed")
        standby = assess_live_session_acceptance(base_health, {**healthy_status, "session_active": False}, now=now)
        self.assertEqual(standby["state"], "standby")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.json"
            write_live_session_acceptance(passed, path=path)
            self.assertEqual(read_live_session_acceptance(path)["state"], "passed")
    def test_transfer_scope_is_evidence_only_and_dependency_ordered(self) -> None:
        names = [table.name for table in TRANSFER_TABLES]
        self.assertLess(names.index("intraday_scan_runs"), names.index("intraday_signal_events"))
        self.assertLess(
            names.index("ten_day_leader_rotation_runs"),
            names.index("ten_day_leader_rotation_intraday_observations"),
        )
        self.assertNotIn("runtime_leases", names)
        self.assertNotIn("intraday_alert_deliveries", names)
        self.assertNotIn("recommendations", names)

    def test_cursor_is_timezone_aware_overlapped_and_bounded(self) -> None:
        now = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
        self.assertEqual(parse_since("2026-08-24T09:00:00Z", now=now), now - timedelta(minutes=65))
        self.assertEqual(parse_checkpoint("2026-08-24T09:00:00Z", now=now), now - timedelta(hours=1))
        self.assertEqual(parse_since("", now=now), now - timedelta(days=30))
        self.assertEqual(parse_since("2020-01-01T00:00:00Z", now=now), now - timedelta(days=30))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            parse_since("2026-08-24T09:00:00", now=now)
        with self.assertRaisesRegex(ValueError, "future"):
            parse_since("2026-08-24T10:06:00Z", now=now)

    def test_change_sequence_is_integer_and_replay_window_is_bounded(self) -> None:
        self.assertEqual(parse_sequence(None), 0)
        self.assertEqual(parse_sequence("0042"), 42)
        self.assertGreater(CHANGE_REPLAY_WINDOW, 0)
        self.assertLess(CHANGE_REPLAY_WINDOW, CHANGE_PAGE_SIZE)
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            parse_sequence("4.2")
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            parse_sequence("-1")

    def test_restricted_edge_export_command_accepts_both_utc_spellings(self) -> None:
        self.assertEqual(
            parse_restricted_export_command("export-since 2026-08-24T09:00:00Z"),
            ("export-since", "2026-08-24T09:00:00+00:00"),
        )
        self.assertEqual(
            parse_restricted_export_command("export-since 2026-08-24T09:00:00+00:00"),
            ("export-since", "2026-08-24T09:00:00+00:00"),
        )
        self.assertEqual(parse_restricted_export_command("export-changes 0042"), ("export-changes", 42))
        for value in ("", "export-since", "export-since 2026-08-24", "export-changes 4;id"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "usage|timezone-aware|non-negative"):
                parse_restricted_export_command(value)

    def test_legacy_cursor_upgrades_to_a_zero_sequence(self) -> None:
        with TemporaryDirectory() as directory:
            cursor = Path(directory) / "cursor.json"
            cursor.write_text(json.dumps({"checkpoint": "2026-08-24T09:50:00Z"}), encoding="utf-8")
            payload = read_cursor_payload(cursor)
        self.assertEqual(payload["sequence"], 0)
        self.assertEqual(payload["checkpoint"], "2026-08-24T09:50:00+00:00")

    def test_pull_status_keeps_last_success_and_marks_only_current_failures(self) -> None:
        now = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            status_path = Path(directory) / "pull-status.json"
            completed = write_pull_status("completed", path=status_path, now=now)
            failed = write_pull_status("failed", error="forced export rejected", path=status_path, now=now + timedelta(minutes=15))
            recovered = write_pull_status("completed", path=status_path, now=now + timedelta(minutes=30))
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(failed["last_success_at"], now.isoformat())
            self.assertEqual(failed["last_error"], "forced export rejected")
            self.assertEqual(recovered["state"], "completed")
            self.assertNotIn("last_error", recovered)
            self.assertEqual(read_pull_status(status_path)["last_success_at"], (now + timedelta(minutes=30)).isoformat())
        with self.assertRaisesRegex(ValueError, "invalid edge evidence pull state"):
            write_pull_status("unknown")

    def test_catching_up_status_keeps_successful_page_metrics(self) -> None:
        now = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            status_path = Path(directory) / "pull-status.json"
            result = write_pull_status(
                "catching_up", error="bounded catch-up will resume", pages_imported=4,
                rows_imported=12_000, duration_ms=4_321, path=status_path, now=now,
            )
        self.assertEqual(result["state"], "catching_up")
        self.assertEqual(result["last_success_at"], now.isoformat())
        self.assertEqual(result["pages_imported"], 4)
        self.assertEqual(result["rows_imported"], 12_000)
        self.assertEqual(result["duration_ms"], 4_321)

    def test_upsert_uses_declared_key_and_updates_mutable_evidence(self) -> None:
        table = next(item for item in TRANSFER_TABLES if item.name == "intraday_scan_runs")
        statement = upsert_statement(table, ("scan_id", "status", "source_status"))
        self.assertIn('ON CONFLICT ("scan_id") DO UPDATE', statement)
        self.assertIn('"status"=EXCLUDED."status"', statement)
        with self.assertRaisesRegex(ValueError, "invalid columns"):
            upsert_statement(table, ("status",))

    def test_handoff_status_is_local_and_marks_stale_snapshots(self) -> None:
        now = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            cursor = Path(directory) / "cursor.json"
            cursor.write_text(json.dumps({
                "checkpoint": "2026-08-24T09:50:00Z", "imported_at": "2026-08-24T09:45:00Z",
                "sequence": 44, "counts": {"intraday_scan_runs": 1},
                "edge_runtime": {"status": "ok", "runtime_profile": "intraday_edge", "build": {"git_sha": "a1b2c3d", "release": "edge-test"}},
            }), encoding="utf-8")
            ready = edge_evidence_status(cursor, now=now, stale_after_seconds=1800)
            stale = edge_evidence_status(cursor, now=now, stale_after_seconds=60)
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["counts"]["intraday_scan_runs"], 1)
        self.assertEqual(ready["sequence"], 44)
        self.assertEqual(ready["runtime"]["build"]["git_sha"], "a1b2c3d")
        self.assertEqual(stale["state"], "stale")

    def test_handoff_marks_backlog_as_catching_up_instead_of_ready(self) -> None:
        now = datetime(2026, 8, 24, 10, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            cursor = Path(directory) / "cursor.json"
            cursor.write_text(json.dumps({
                "checkpoint": "2026-08-24T09:50:00Z", "imported_at": "2026-08-24T09:59:00Z",
                "sequence": 44, "remote_sequence": 144, "has_more": True,
                "remote_latest_changed_at": "2026-08-24T09:59:30Z",
                "edge_runtime": {"status": "ok", "runtime_profile": "intraday_edge"},
            }), encoding="utf-8")
            status = edge_evidence_status(cursor, now=now, stale_after_seconds=1800)
        self.assertEqual(status["state"], "catching_up")
        self.assertEqual(status["sequence_lag"], 100)
        self.assertTrue(status["has_more"])
        self.assertEqual(status["remote_latest_changed_at"], "2026-08-24T09:59:30+00:00")


if __name__ == "__main__":
    unittest.main()
