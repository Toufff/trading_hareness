from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.edge_evidence_transfer import (
    CHANGE_PAGE_SIZE, CHANGE_REPLAY_WINDOW, TRANSFER_TABLES, edge_evidence_status,
    parse_checkpoint, parse_sequence, parse_since, read_cursor_payload, upsert_statement,
)


class EdgeEvidenceTransferTests(unittest.TestCase):
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

    def test_legacy_cursor_upgrades_to_a_zero_sequence(self) -> None:
        with TemporaryDirectory() as directory:
            cursor = Path(directory) / "cursor.json"
            cursor.write_text(json.dumps({"checkpoint": "2026-08-24T09:50:00Z"}), encoding="utf-8")
            payload = read_cursor_payload(cursor)
        self.assertEqual(payload["sequence"], 0)
        self.assertEqual(payload["checkpoint"], "2026-08-24T09:50:00+00:00")

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
                "edge_runtime": {"status": "ok", "runtime_profile": "intraday_edge"},
            }), encoding="utf-8")
            ready = edge_evidence_status(cursor, now=now, stale_after_seconds=1800)
            stale = edge_evidence_status(cursor, now=now, stale_after_seconds=60)
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["counts"]["intraday_scan_runs"], 1)
        self.assertEqual(ready["sequence"], 44)
        self.assertEqual(stale["state"], "stale")


if __name__ == "__main__":
    unittest.main()
