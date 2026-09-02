"""Real-PostgreSQL coverage for post-close/leader-rotation candidate settlement.

Neither post_close_strategy_candidates nor ten_day_leader_rotation_candidates
had any outcome linkage before this: nothing ever measured what happened
after either strategy proposed a symbol, so neither could accumulate the
evidence its own promotion gate requires.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.main import DailyBar, db, upsert_bar
from app.post_close_candidate_outcomes import (
    POST_CLOSE_STRATEGY_CANDIDATES,
    TEN_DAY_LEADER_ROTATION_CANDIDATES,
    settle_candidate_outcomes,
)


def _seed_trading_calendar(connection, dates) -> None:
    """``resolve_exit`` reads the exit session off the trade calendar, not off
    the symbol's own bar sequence, so every fixture date must be marked open."""
    for trading_date in dates:
        connection.execute(
            """INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,provider,available_at)
               VALUES('SSE',%s,true,'p0-candidate-outcome-test',%s)
               ON CONFLICT(exchange,calendar_date) DO UPDATE SET is_open=true""",
            (trading_date, datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc)),
        )


def _clear_trading_calendar(connection, dates) -> None:
    for trading_date in dates:
        connection.execute(
            "DELETE FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=%s", (trading_date,),
        )


def _seed_bars(connection, symbol: str, run_date: date, *, entry_locked: bool) -> None:
    entry_date = run_date + timedelta(days=1)
    prices = {entry_date: Decimal("10.00"), entry_date + timedelta(days=1): Decimal("10.20")}
    for offset in range(2, 10):
        prices[entry_date + timedelta(days=offset)] = Decimal("10.20") + Decimal(offset) * Decimal("0.03")
    _seed_trading_calendar(connection, prices.keys())
    for trading_date, close in prices.items():
        entry_open = close * Decimal("1.10") if entry_locked and trading_date == entry_date else close
        upsert_bar(connection, DailyBar(
            symbol=symbol, trading_date=trading_date, open=entry_open, high=max(entry_open, close) * Decimal("1.01"),
            low=min(entry_open, close) * Decimal("0.99"), close=close, adj_factor=Decimal("1.0"), is_suspended=False,
            limit_up=close * Decimal("1.10") if trading_date == entry_date else None,
            limit_down=close * Decimal("0.90") if trading_date == entry_date else None,
            source="p0-candidate-outcome-test", available_at=datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc),
        ))


def _calendar_dates(run_date: date) -> list[date]:
    entry_date = run_date + timedelta(days=1)
    return [entry_date + timedelta(days=offset) for offset in range(10)]


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class PostCloseCandidateOutcomeIntegrationTests(unittest.TestCase):
    symbol = "999990.SZ"
    run_date = date(2099, 1, 2)
    as_of_date = date(2099, 1, 20)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.post_close_strategy_candidate_outcomes WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.post_close_strategy_candidates WHERE symbol=%s", (self.symbol,))
            _clear_trading_calendar(connection, _calendar_dates(self.run_date))
            connection.execute("DELETE FROM quant.post_close_strategy_runs WHERE model_version=%s", ("p0-candidate-outcome-test",))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def _seed_run_and_candidate(self, connection, *, entry_locked: bool):
        _seed_bars(connection, self.symbol, self.run_date, entry_locked=entry_locked)
        run = connection.execute(
            """INSERT INTO quant.post_close_strategy_runs(run_key,as_of_date,model_version,status)
               VALUES(%s,%s,'p0-candidate-outcome-test','completed') RETURNING run_id""",
            (f"p0-candidate-outcome-test-{entry_locked}", self.run_date),
        ).fetchone()
        connection.execute(
            """INSERT INTO quant.post_close_strategy_candidates(run_id,rank,symbol,candidate_type,score)
               VALUES(%s,1,%s,'fresh_start_15d',80)""",
            (run["run_id"], self.symbol),
        )
        return run["run_id"]

    def test_locked_limit_up_open_leaves_the_candidate_unsettled(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                self._seed_run_and_candidate(connection, entry_locked=True)
            # settle_candidate_outcomes settles every eligible real candidate in
            # the table, not just this test's row, so the return count is not
            # asserted here - only that this reserved symbol stays unsettled.
            with db.transaction() as connection:
                settle_candidate_outcomes(connection, self.as_of_date, POST_CLOSE_STRATEGY_CANDIDATES)
            with db.transaction() as connection:
                outcome = connection.execute(
                    "SELECT * FROM quant.post_close_strategy_candidate_outcomes WHERE symbol=%s", (self.symbol,)
                ).fetchone()
            self.assertIsNone(outcome)
        finally:
            self._cleanup()

    def test_fillable_open_settles_at_next_session_open(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                self._seed_run_and_candidate(connection, entry_locked=False)
            with db.transaction() as connection:
                settled = settle_candidate_outcomes(connection, self.as_of_date, POST_CLOSE_STRATEGY_CANDIDATES)
            self.assertGreaterEqual(settled, 1)
            with db.transaction() as connection:
                outcome = connection.execute(
                    "SELECT * FROM quant.post_close_strategy_candidate_outcomes WHERE symbol=%s", (self.symbol,)
                ).fetchone()
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["tradability"], "observed_open")
            self.assertEqual(Decimal(outcome["entry_price"]), Decimal("10.00"))
            self.assertEqual(outcome["horizon_days"], 10)
            self.assertIsNotNone(outcome["raw_return"])
            # Re-running settlement must upsert the same row, not duplicate it.
            with db.transaction() as connection:
                settle_candidate_outcomes(connection, self.as_of_date, POST_CLOSE_STRATEGY_CANDIDATES)
            with db.transaction() as connection:
                count = connection.execute(
                    "SELECT count(*)::int AS n FROM quant.post_close_strategy_candidate_outcomes WHERE symbol=%s", (self.symbol,)
                ).fetchone()["n"]
            self.assertEqual(count, 1)
        finally:
            self._cleanup()


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class TenDayLeaderRotationCandidateOutcomeIntegrationTests(unittest.TestCase):
    symbol = "999989.SZ"
    run_date = date(2099, 1, 2)
    as_of_date = date(2099, 1, 20)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.ten_day_leader_rotation_candidate_outcomes WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.ten_day_leader_rotation_candidates WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.ten_day_leader_rotation_runs WHERE model_version=%s", ("p0-candidate-outcome-test",))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))
            _clear_trading_calendar(connection, _calendar_dates(self.run_date))

    def test_fillable_open_settles_a_leader_candidate(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                _seed_bars(connection, self.symbol, self.run_date, entry_locked=False)
                run = connection.execute(
                    """INSERT INTO quant.ten_day_leader_rotation_runs(run_key,as_of_date,model_version,status)
                       VALUES('p0-candidate-outcome-test',%s,'p0-candidate-outcome-test','completed') RETURNING run_id""",
                    (self.run_date,),
                ).fetchone()
                connection.execute(
                    """INSERT INTO quant.ten_day_leader_rotation_candidates(
                            run_id,board,board_rank,symbol,ten_day_return_pct,current_return_pct,shadow_state)
                       VALUES(%s,'main',1,%s,12.5,12.5,'observed')""",
                    (run["run_id"], self.symbol),
                )
            with db.transaction() as connection:
                settled = settle_candidate_outcomes(connection, self.as_of_date, TEN_DAY_LEADER_ROTATION_CANDIDATES)
            self.assertGreaterEqual(settled, 1)
            with db.transaction() as connection:
                outcome = connection.execute(
                    "SELECT * FROM quant.ten_day_leader_rotation_candidate_outcomes WHERE symbol=%s", (self.symbol,)
                ).fetchone()
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["tradability"], "observed_open")
        finally:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()
