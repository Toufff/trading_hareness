"""Coverage for the per-member N+1 fix in feature_snapshot_repository.

Previously each universe member triggered its own ``canonical_bars_daily``
and ``daily_fundamentals`` lookup (2 extra queries x member count).  Both are
now one ``symbol = ANY(%s)`` query for the whole universe, grouped in memory.
"""

from __future__ import annotations

from datetime import date
import unittest

from app.feature_snapshot_repository import materialize_feature_snapshot


class _Result:
    def __init__(self, *, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return None


class _RecordingConnection:
    def __init__(self, members, bars_rows, fundamentals_rows, lifecycle_rows=None):
        self.members = members
        self.bars_rows = bars_rows
        self.fundamentals_rows = fundamentals_rows
        self.lifecycle_rows = lifecycle_rows or []
        self.calls: list[str] = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append(normalized)
        if "FROM quant.universe_members" in normalized:
            return _Result(rows=self.members)
        if "FROM quant.canonical_bars_daily" in normalized:
            return _Result(rows=self.bars_rows)
        if "FROM quant.daily_fundamentals" in normalized:
            return _Result(rows=self.fundamentals_rows)
        if "FROM quant.instrument_lifecycle_evidence" in normalized:
            return _Result(rows=self.lifecycle_rows)
        if "INSERT INTO quant.feature_snapshots" in normalized:
            return _Result()
        raise AssertionError(f"unexpected SQL: {normalized}")


class FeatureSnapshotBatchingTests(unittest.TestCase):
    def test_bars_and_fundamentals_are_read_in_exactly_one_query_each_for_the_whole_universe(self) -> None:
        members = [
            {"symbol": "000001.SZ", "name": "A", "industry": "I1", "is_st": False},
            {"symbol": "600000.SH", "name": "B", "industry": "I2", "is_st": False},
        ]
        bars_rows = []
        for symbol in ("000001.SZ", "600000.SH"):
            for day in range(1, 22):
                bars_rows.append({
                    "symbol": symbol, "trading_date": date(2026, 1, day), "close": 10 + day,
                    "high": 10 + day, "low": 10 + day, "volume": 100, "amount": 1000,
                    "adj_factor": 1.0, "is_suspended": False, "limit_up": None, "limit_down": None,
                    "selected_provider": "super_sdk",
                })
        fundamentals_rows = [
            {"symbol": "000001.SZ", "turnover_rate": 1.0, "volume_ratio": 1.0, "pe": 10.0, "pb": 1.0,
             "total_mv": 1000.0, "circ_mv": 900.0},
        ]
        connection = _RecordingConnection(members, bars_rows, fundamentals_rows)
        result = materialize_feature_snapshot(
            connection, date(2026, 1, 22), "core", feature_version="p0-test",
            number=float, market_regime=lambda *_: "neutral",
            analyst_text_factor_summary=lambda *_: {"market": {}},
            latest_tushare_row=lambda *_: None, analyst_feature=lambda *_: {},
        )
        self.assertEqual(len(result["items"]), 2)
        bars_queries = [sql for sql in connection.calls if "FROM quant.canonical_bars_daily" in sql]
        fundamentals_queries = [sql for sql in connection.calls if "FROM quant.daily_fundamentals" in sql]
        self.assertEqual(len(bars_queries), 1)
        self.assertEqual(len(fundamentals_queries), 1)
        self.assertIn("symbol=ANY(%s)", bars_queries[0])
        self.assertIn("symbol=ANY(%s)", fundamentals_queries[0])
        by_symbol = {item["symbol"]: item for item in result["items"]}
        self.assertIn("fundamentals", by_symbol["000001.SZ"]["features"])
        self.assertNotIn("fundamentals", by_symbol["600000.SH"]["features"])
        self.assertIn("missing_fundamentals", by_symbol["600000.SH"]["quality_flags"])
        # instrument_lifecycle_evidence is read once for the whole universe too.
        lifecycle_queries = [sql for sql in connection.calls if "FROM quant.instrument_lifecycle_evidence" in sql]
        self.assertEqual(len(lifecycle_queries), 1)
        self.assertIn("symbol=ANY(%s)", lifecycle_queries[0])


class FeatureSnapshotPointInTimeIsStTests(unittest.TestCase):
    """ST status must reflect what was known as of the replay date, not the
    current ``instruments.is_st`` value, whenever point-in-time evidence
    exists; a symbol without any lifecycle evidence keeps the current-state
    fallback instead of silently defaulting to not-ST."""

    def _members_and_bars(self):
        members = [
            {"symbol": "000001.SZ", "name": "A", "industry": "I1", "is_st": False},
            {"symbol": "600000.SH", "name": "B", "industry": "I2", "is_st": True},
        ]
        bars_rows = []
        for symbol in ("000001.SZ", "600000.SH"):
            for day in range(1, 22):
                bars_rows.append({
                    "symbol": symbol, "trading_date": date(2026, 1, day), "close": 10 + day,
                    "high": 10 + day, "low": 10 + day, "volume": 100, "amount": 1000,
                    "adj_factor": 1.0, "is_suspended": False, "limit_up": None, "limit_down": None,
                    "selected_provider": "super_sdk",
                })
        return members, bars_rows

    def test_lifecycle_evidence_overrides_the_current_state_join(self) -> None:
        members, bars_rows = self._members_and_bars()
        # As-of the replay date, 000001.SZ *was* ST even though it is not
        # today, and 600000.SH was not ST even though it is today.
        lifecycle_rows = [
            {"symbol": "000001.SZ", "is_st": True},
            {"symbol": "600000.SH", "is_st": False},
        ]
        connection = _RecordingConnection(members, bars_rows, [], lifecycle_rows)
        result = materialize_feature_snapshot(
            connection, date(2026, 1, 22), "core", feature_version="p0-test",
            number=float, market_regime=lambda *_: "neutral",
            analyst_text_factor_summary=lambda *_: {"market": {}},
            latest_tushare_row=lambda *_: None, analyst_feature=lambda *_: {},
        )
        flags_by_symbol = {item["symbol"]: item["quality_flags"] for item in result["items"]}
        self.assertIn("ST", flags_by_symbol["000001.SZ"])
        self.assertNotIn("ST", flags_by_symbol["600000.SH"])

    def test_symbol_without_lifecycle_evidence_keeps_the_current_state_fallback(self) -> None:
        members, bars_rows = self._members_and_bars()
        connection = _RecordingConnection(members, bars_rows, [], lifecycle_rows=[])
        result = materialize_feature_snapshot(
            connection, date(2026, 1, 22), "core", feature_version="p0-test",
            number=float, market_regime=lambda *_: "neutral",
            analyst_text_factor_summary=lambda *_: {"market": {}},
            latest_tushare_row=lambda *_: None, analyst_feature=lambda *_: {},
        )
        flags_by_symbol = {item["symbol"]: item["quality_flags"] for item in result["items"]}
        self.assertNotIn("ST", flags_by_symbol["000001.SZ"])
        self.assertIn("ST", flags_by_symbol["600000.SH"])


if __name__ == "__main__":
    unittest.main()
