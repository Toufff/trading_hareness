from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app.feature_snapshot_repository import materialize_feature_snapshot


class _Result:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self._rows = rows or []

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _FilteringConnection:
    """A fake that actually applies the query's own trading_date/available_at bounds.

    Unlike a naive substring-matching stub, this proves the SQL text change
    (``trading_date<%s AND available_at<=%s``) really changes what rows are
    returned, not just that the query string contains the right tokens.
    """

    def __init__(self, bars: list[dict[str, object]], writes: list[tuple[str, tuple[object, ...]]]) -> None:
        self._bars = bars
        self._writes = writes

    def execute(self, sql: str, params: tuple[object, ...]) -> _Result:
        if "FROM quant.universe_members" in sql:
            return _Result([{"symbol": "000001.SZ", "name": "Test", "industry": "Test", "is_st": False}])
        if "FROM quant.canonical_bars_daily" in sql:
            _symbols, as_of_date, observed_at = params
            matched = [
                bar for bar in self._bars
                if bar["trading_date"] < as_of_date and bar["available_at"] <= observed_at
            ]
            return _Result(sorted(matched, key=lambda bar: bar["trading_date"], reverse=True))
        if "FROM quant.daily_fundamentals" in sql:
            return _Result([])
        if "FROM quant.instrument_lifecycle_evidence" in sql:
            return _Result([])
        if "INSERT INTO quant.feature_snapshots" in sql:
            self._writes.append((sql, params))
            return _Result([])
        raise AssertionError(f"unexpected SQL: {sql}")


def _bars(count: int, *, available_same_day: bool = True) -> list[dict[str, object]]:
    bars = []
    for index in range(count):
        trading_date = date(2026, 1, index + 1)
        available_at = (
            datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc)
            if available_same_day
            else datetime(2099, 1, 1, tzinfo=timezone.utc)  # a "future backfill" available_at
        )
        bars.append({
            "symbol": "000001.SZ", "trading_date": trading_date, "close": 10 + index,
            "high": 10 + index, "low": 10 + index, "volume": 100, "amount": 1000,
            "adj_factor": 1.0, "is_suspended": False, "limit_up": None, "limit_down": None,
            "selected_provider": "super_sdk", "available_at": available_at,
        })
    return bars


class MaterializeFeatureSnapshotPitTests(unittest.TestCase):
    def test_same_day_bar_is_excluded_from_its_own_snapshot(self) -> None:
        # 25 bars dated 2026-01-01..25; a snapshot "as of" 2026-01-25 must not
        # see its own day's close, only the 24 strictly-prior sessions.
        bars = _bars(25)
        writes: list[tuple[str, tuple[object, ...]]] = []
        connection = _FilteringConnection(bars, writes)
        result = materialize_feature_snapshot(
            connection, date(2026, 1, 25), "core", feature_version="pit-test",
            number=float, market_regime=lambda *_: "neutral",
            analyst_text_factor_summary=lambda *_: {"market": {}},
            latest_tushare_row=lambda *_: None, analyst_feature=lambda *_: {},
        )
        feature = result["items"][0]["features"]
        # Bar index 23 (2026-01-24) is the latest strictly-prior session;
        # its close is 10+23=33, not the excluded same-day 10+24=34.
        self.assertEqual(feature["close"], 33.0)
        self.assertEqual(feature["market_data_date"], "2026-01-24")

    def test_a_future_backfill_available_at_is_excluded_from_a_historical_replay(self) -> None:
        # Every bar's available_at is stamped 2099, simulating a later
        # correction; a replay observing as of 2026-01-25 must not see it.
        bars = _bars(25, available_same_day=False)
        writes: list[tuple[str, tuple[object, ...]]] = []
        connection = _FilteringConnection(bars, writes)
        result = materialize_feature_snapshot(
            connection, date(2026, 1, 25), "core", feature_version="pit-test",
            number=float, market_regime=lambda *_: "neutral",
            analyst_text_factor_summary=lambda *_: {"market": {}},
            latest_tushare_row=lambda *_: None, analyst_feature=lambda *_: {},
        )
        feature = result["items"][0]["features"]
        self.assertEqual(feature["bar_count"], 0)
        self.assertIn("missing_market_data", result["items"][0]["quality_flags"])


if __name__ == "__main__":
    unittest.main()
