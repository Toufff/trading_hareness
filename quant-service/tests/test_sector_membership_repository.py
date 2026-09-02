"""Pure and SQL-boundary checks for sector membership provenance."""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from app.sector_membership_repository import (
    OBSERVED_SNAPSHOT,
    PROVIDER_INTERVAL,
    membership_interval,
    persist_observed_snapshot,
    persist_ths_snapshot,
    point_in_time_membership_predicate,
)


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class SectorMembershipRepositoryTests(unittest.TestCase):
    def test_missing_provider_start_becomes_observed_snapshot_not_1900(self) -> None:
        observed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        start, end, start_basis, end_basis = membership_interval(
            {"con_code": "000001.SZ", "in_date": None, "out_date": None}, observed_at,
            parse_date=lambda value: date.fromisoformat(value) if value else None,
        )
        self.assertEqual(start, date(2026, 8, 31))
        self.assertIsNone(end)
        self.assertEqual((start_basis, end_basis), (OBSERVED_SNAPSHOT, OBSERVED_SNAPSHOT))

    def test_provider_interval_keeps_source_dates(self) -> None:
        observed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        start, end, start_basis, end_basis = membership_interval(
            {"in_date": "2020-01-02", "out_date": "2025-03-04"}, observed_at,
            parse_date=lambda value: date.fromisoformat(value) if value else None,
        )
        self.assertEqual((start, end), (date(2020, 1, 2), date(2025, 3, 4)))
        self.assertEqual((start_basis, end_basis), (PROVIDER_INTERVAL, PROVIDER_INTERVAL))

    def test_strict_predicate_excludes_legacy_and_bounds_known_time(self) -> None:
        sql = point_in_time_membership_predicate("member")
        self.assertIn("effective_from_basis IN ('provider_interval','observed_snapshot')", sql)
        self.assertIn("member.known_at <=", sql)
        self.assertIn("17:00:00", sql)
        self.assertNotIn("1900", sql)


class PersistThsSnapshotBatchingTests(unittest.TestCase):
    """persist_ths_snapshot previously ran one INSERT per constituent; it is
    now one batched upsert regardless of member count."""

    def test_valid_members_are_written_in_one_statement(self) -> None:
        connection = _RecordingConnection()
        observed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        active = persist_ths_snapshot(
            connection, "ths_concept_flow", "885001.TI",
            [
                {"con_code": "000001.SZ", "in_date": "20200102"},
                {"con_code": "600000.SH", "in_date": "20200103"},
                {"con_code": "bad-code"},
            ],
            "tushare_super_sdk", observed_at,
            ensure_instrument=lambda *_a: None,
            parse_date=lambda value: date(2020, 1, 2) if value == "20200102" else (
                date(2020, 1, 3) if value == "20200103" else None
            ),
        )
        self.assertEqual(active, 2)
        insert_calls = [(sql, params) for sql, params in connection.calls
                        if "INSERT INTO quant.sector_membership_history" in sql]
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertEqual(set(params["symbols"]), {"000001.SZ", "600000.SH"})
        update_calls = [sql for sql, _params in connection.calls if sql.startswith("UPDATE")]
        self.assertEqual(len(update_calls), 1)

    def test_no_valid_members_issues_no_insert(self) -> None:
        connection = _RecordingConnection()
        observed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        active = persist_ths_snapshot(
            connection, "ths_concept_flow", "885001.TI", [{"con_code": "bad"}], "tushare_super_sdk", observed_at,
            ensure_instrument=lambda *_a: None, parse_date=lambda _value: None,
        )
        self.assertEqual(active, 0)
        self.assertFalse(any("INSERT INTO quant.sector_membership_history" in sql for sql, _params in connection.calls))


class PersistObservedSnapshotBatchingTests(unittest.TestCase):
    def test_members_are_written_in_one_statement(self) -> None:
        connection = _RecordingConnection()
        observed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        stored = persist_observed_snapshot(
            connection, "ths_concept_flow", "885001.TI",
            [{"symbol": "000001.SZ"}, {"symbol": "600000.SH"}, {"symbol": None}],
            "tushare_super_sdk", observed_at,
            member_symbol=lambda row: row.get("symbol"),
            ensure_instrument=lambda *_a: None,
        )
        self.assertEqual(stored, 2)
        insert_calls = [(sql, params) for sql, params in connection.calls
                        if "INSERT INTO quant.sector_membership_history" in sql]
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertEqual(set(params["symbols"]), {"000001.SZ", "600000.SH"})

    def test_duplicate_symbols_are_deduplicated_last_wins(self) -> None:
        connection = _RecordingConnection()
        observed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        stored = persist_observed_snapshot(
            connection, "ths_concept_flow", "885001.TI",
            [{"symbol": "000001.SZ", "v": 1}, {"symbol": "000001.SZ", "v": 2}],
            "tushare_super_sdk", observed_at,
            member_symbol=lambda row: row.get("symbol"),
            ensure_instrument=lambda *_a: None,
        )
        self.assertEqual(stored, 2)
        _sql, params = [(sql, params) for sql, params in connection.calls
                        if "INSERT INTO quant.sector_membership_history" in sql][0]
        self.assertEqual(len(params["symbols"]), 1)
        self.assertIn('"v": 2', params["raws"][0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
