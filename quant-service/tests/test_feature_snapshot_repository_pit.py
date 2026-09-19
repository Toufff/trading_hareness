from __future__ import annotations

import unittest
import re
from datetime import date, datetime, time, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from app import outcome_recomputation
from app.feature_snapshot_repository import materialize_feature_snapshot

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Result:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self._rows = rows or []

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _FilteringConnection:
    """A fake that actually applies the query's own trading_date/available_at bounds.

    Unlike a naive substring-matching stub, this proves the SQL predicate
    (``trading_date<=%s AND available_at<=%s AND (trading_date<%s OR
    available_at>=%s)``) really changes what rows are returned, not just that
    the query string contains the right tokens.  The fake asserts the exact
    predicate text so a silent change back to ``<`` fails here.
    """

    def __init__(self, bars: list[dict[str, object]], writes: list[tuple[str, tuple[object, ...]]]) -> None:
        self._bars = bars
        self._writes = writes

    def execute(self, sql: str, params: tuple[object, ...]) -> _Result:
        if "FROM quant.universe_members" in sql:
            return _Result([{"symbol": "000001.SZ", "name": "Test", "industry": "Test", "is_st": False}])
        if "FROM quant.canonical_bars_daily" in sql:
            assert re.search(
                r"trading_date<=%s AND available_at<=%s\s+AND \(trading_date<%s OR available_at>=%s\)", sql,
            ), sql
            _symbols, as_of_date, observed_at, same_day_boundary, settled_at = params
            matched = [
                bar for bar in self._bars
                if bar["trading_date"] <= as_of_date and bar["available_at"] <= observed_at
                and (bar["trading_date"] < same_day_boundary or bar["available_at"] >= settled_at)
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


def _settled_at(trading_date: date) -> datetime:
    """A realistic post-close availability stamp (15:30 Asia/Shanghai)."""
    return datetime.combine(trading_date, time(15, 30), tzinfo=SHANGHAI)


def _bars(count: int, *, available_same_day: bool = True,
          last_available_at: datetime | None = None) -> list[dict[str, object]]:
    bars = []
    for index in range(count):
        trading_date = date(2026, 1, index + 1)
        available_at = (
            _settled_at(trading_date)
            if available_same_day
            else datetime(2099, 1, 1, tzinfo=timezone.utc)  # a "future backfill" available_at
        )
        if last_available_at is not None and index == count - 1:
            available_at = last_available_at
        bars.append({
            "symbol": "000001.SZ", "trading_date": trading_date, "close": 10 + index,
            "high": 10 + index, "low": 10 + index, "volume": 100, "amount": 1000,
            "adj_factor": 1.0, "is_suspended": False, "limit_up": None, "limit_down": None,
            "selected_provider": "super_sdk", "available_at": available_at,
        })
    return bars


def _materialize(bars: list[dict[str, object]], as_of_date: date,
                 observed_at: datetime | None = None, connection: object | None = None) -> dict[str, object]:
    return materialize_feature_snapshot(
        connection or _FilteringConnection(bars, []), as_of_date, "core", feature_version="pit-test",
        number=float, market_regime=lambda *_: "neutral",
        analyst_text_factor_summary=lambda *_: {"market": {}},
        latest_tushare_row=lambda *_: None, analyst_feature=lambda *_: {},
        observed_at=observed_at,
    )


class MaterializeFeatureSnapshotPitTests(unittest.TestCase):
    def test_settled_same_day_bar_is_included_in_its_own_snapshot(self) -> None:
        # 2026-09-19 user decision: the post-close run for session D scores on
        # D's own close.  25 bars dated 2026-01-01..25, each available at
        # 15:30 on its own date; the snapshot as of 2026-01-25 sees the 25th.
        result = _materialize(_bars(25), date(2026, 1, 25))
        feature = result["items"][0]["features"]
        self.assertEqual(feature["close"], 34.0)
        self.assertEqual(feature["market_data_date"], "2026-01-25")
        self.assertEqual(feature["market_data_date"], result["as_of_date"])
        self.assertEqual(feature["bar_count"], 25)

    def test_same_day_bar_available_after_observed_at_is_excluded(self) -> None:
        # The same-day bar exists but was only published after the replay's
        # observation time, so the snapshot falls back to the prior session.
        observed_at = datetime(2026, 1, 25, 16, 0, tzinfo=SHANGHAI)
        bars = _bars(25, last_available_at=datetime(2026, 1, 25, 17, 0, tzinfo=SHANGHAI))
        feature = _materialize(bars, date(2026, 1, 25), observed_at)["items"][0]["features"]
        self.assertEqual(feature["close"], 33.0)
        self.assertEqual(feature["market_data_date"], "2026-01-24")

    def test_unsettled_intraday_same_day_bar_is_excluded_even_when_already_available(self) -> None:
        # An intraday caller (dashboard button, as_of defaults to today) at
        # 11:00 must not read a still-running bar written at 10:30, even though
        # 10:30 <= observed_at: it is not a close until the session settles.
        observed_at = datetime(2026, 1, 25, 11, 0, tzinfo=SHANGHAI)
        bars = _bars(25, last_available_at=datetime(2026, 1, 25, 10, 30, tzinfo=SHANGHAI))
        for explicit_or_default in (observed_at, None):
            feature = _materialize(bars, date(2026, 1, 25), explicit_or_default)["items"][0]["features"]
            self.assertEqual(feature["market_data_date"], "2026-01-24")

    def test_a_future_backfill_available_at_is_excluded_from_a_historical_replay(self) -> None:
        # Every bar's available_at is stamped 2099, simulating a later
        # correction; a replay observing as of 2026-01-25 must not see it.
        result = _materialize(_bars(25, available_same_day=False), date(2026, 1, 25))
        feature = result["items"][0]["features"]
        self.assertEqual(feature["bar_count"], 0)
        self.assertIn("missing_market_data", result["items"][0]["quality_flags"])

    def test_same_day_rule_applies_to_fundamentals_and_lifecycle_reads(self) -> None:
        recorded: list[tuple[str, tuple[object, ...]]] = []
        inner = _FilteringConnection(_bars(25), [])

        class _Recording:
            def execute(self, sql: str, params: tuple[object, ...]):
                recorded.append((sql, params))
                return inner.execute(sql, params)

        _materialize([], date(2026, 1, 25), connection=_Recording())
        fundamentals = next(item for item in recorded if "FROM quant.daily_fundamentals" in item[0])
        self.assertIn("trading_date<=%s AND available_at<=%s", fundamentals[0])
        self.assertIn("(trading_date<%s OR available_at>=%s)", fundamentals[0])
        self.assertEqual(fundamentals[1][4], datetime(2026, 1, 25, 15, 5, tzinfo=SHANGHAI))
        lifecycle = next(item for item in recorded if "FROM quant.instrument_lifecycle_evidence" in item[0])
        self.assertIn("status_date<=%s AND available_at<=%s", lifecycle[0])


class _OutcomeEntryConnection:
    """Serves ``recompute``'s reads and applies the entry query's own date operator."""

    def __init__(self, run_date: date, bar_dates: list[date]) -> None:
        self._run_date = run_date
        self._bar_dates = bar_dates
        self.entry_sql: list[str] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _Result:
        if "FROM quant.analyst_claims" in sql:
            return _Result([])
        if "FROM quant.recommendation_runs" in sql:
            return _Result([{"run_id": "run", "run_date": self._run_date, "symbol": "000001.SZ",
                             "direction": 1, "horizon_days": 5}])
        if "FROM quant.canonical_bars_daily" in sql and "ORDER BY trading_date LIMIT 1" in sql:
            self.entry_sql.append(sql)
            match = re.search(r"trading_date(>=?)%s AND trading_date<=%s", sql)
            assert match, sql
            _symbol, run_date, as_of_date = params
            strict = match.group(1) == ">"
            eligible = [day for day in self._bar_dates
                        if (day > run_date if strict else day >= run_date) and day <= as_of_date]
            if not eligible:
                return _Result([])
            return _Result([{"trading_date": min(eligible), "open": 10, "is_suspended": False,
                             "limit_up": None, "limit_down": None}])
        return _Result([])


class _OutcomeDb:
    def __init__(self, connection: _OutcomeEntryConnection) -> None:
        self._connection = connection

    def transaction(self):
        connection = self._connection

        class _Context:
            def __enter__(self):
                return connection

            def __exit__(self, *_exc):
                return False

        return _Context()


class SameDayFeatureNextSessionEntryTests(unittest.TestCase):
    """The same-day feature rule is only look-ahead free because outcome
    scoring enters at the NEXT session's open.  Pin both halves together."""

    def _recompute(self, run_date: date, as_of: date, bar_dates: list[date]) -> tuple[list[date], list[str]]:
        connection = _OutcomeEntryConnection(run_date, bar_dates)
        entries: list[date] = []

        def _capture(_connection, _symbol, entry_date, _horizon, _as_of):
            entries.append(entry_date)
            return {"status": "pending"}

        with mock.patch.object(outcome_recomputation, "resolve_exit", _capture):
            outcome_recomputation.recompute(
                as_of, cn_today=lambda: as_of, db=_OutcomeDb(connection),
                recompute_intraday_signal_outcomes=lambda _as_of: {"outcome_rows": 0},
            )
        return entries, connection.entry_sql

    def test_recommendation_entry_is_the_first_bar_strictly_after_the_run_date(self) -> None:
        run_date = date(2026, 1, 25)
        next_session = date(2026, 1, 26)
        feature = _materialize(_bars(25), run_date)["items"][0]["features"]
        self.assertEqual(feature["market_data_date"], str(run_date))

        entries, entry_sql = self._recompute(
            run_date, next_session, [run_date - timedelta(days=1), run_date, next_session])
        self.assertEqual(len(entry_sql), 1)
        self.assertIn("trading_date>%s AND trading_date<=%s", entry_sql[0])
        self.assertEqual(entries, [next_session])
        self.assertLess(date.fromisoformat(feature["market_data_date"]), entries[0])

    def test_no_entry_exists_until_the_next_session_has_a_bar(self) -> None:
        run_date = date(2026, 1, 25)
        entries, _ = self._recompute(run_date, run_date, [run_date - timedelta(days=1), run_date])
        self.assertEqual(entries, [], "the run date's own bar must never be used as the entry session")

if __name__ == "__main__":
    unittest.main()
