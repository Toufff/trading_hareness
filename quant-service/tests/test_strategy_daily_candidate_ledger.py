"""Real-PostgreSQL coverage for the unified cross-strategy candidate ledger."""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.liquidity_screen import MINIMUM_MEDIAN_DAILY_AMOUNT
from app.main import DailyBar, db, upsert_bar
from app.strategy_daily_candidate_ledger import (
    materialize_ledger,
    materialize_limit_linkage_candidates,
    materialize_post_close_candidates,
    settle_ledger_outcomes,
)


def _seed_bars(connection, symbol: str, run_date: date, *, amount: Decimal = Decimal("50000000")) -> date:
    entry_date = run_date + timedelta(days=1)
    # The candidate's own discovery-date bar (and a short trailing window) must
    # exist too: liquidity is judged as of run_date, not the forward window.
    prices = {run_date - timedelta(days=1): Decimal("9.90"), run_date: Decimal("10.00"),
              entry_date: Decimal("10.00"), entry_date + timedelta(days=1): Decimal("10.20")}
    for offset in range(2, 10):
        prices[entry_date + timedelta(days=offset)] = Decimal("10.20") + Decimal(offset) * Decimal("0.03")
    for trading_date, close in prices.items():
        upsert_bar(connection, DailyBar(
            symbol=symbol, trading_date=trading_date, open=close, high=close * Decimal("1.01"), low=close * Decimal("0.99"),
            close=close, volume=Decimal("1000000"), amount=amount, adj_factor=Decimal("1.0"), is_suspended=False,
            source="p0-ledger-test", available_at=datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc),
        ))
    return entry_date


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class PostCloseLedgerMaterializationTests(unittest.TestCase):
    symbol_liquid = "999988.SZ"
    symbol_illiquid = "999987.SZ"
    run_date = date(2099, 1, 2)
    as_of_date = date(2099, 1, 20)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            for symbol in (self.symbol_liquid, self.symbol_illiquid):
                connection.execute("DELETE FROM quant.strategy_daily_candidate_outcomes WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.strategy_daily_candidates WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.post_close_strategy_candidates WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (symbol,))
            connection.execute("DELETE FROM quant.post_close_strategy_runs WHERE model_version=%s", ("p0-ledger-test",))

    def test_materializes_with_correct_strategy_key_scale_and_liquidity_flag(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                _seed_bars(connection, self.symbol_liquid, self.run_date, amount=MINIMUM_MEDIAN_DAILY_AMOUNT * 2)
                _seed_bars(connection, self.symbol_illiquid, self.run_date, amount=Decimal("1000"))
                # upsert_bar has no list_date field; set it directly so the liquid
                # fixture actually clears the listing-age screen.
                connection.execute(
                    "UPDATE quant.instruments SET list_date=%s WHERE symbol=ANY(%s)",
                    (date(2000, 1, 1), [self.symbol_liquid, self.symbol_illiquid]),
                )
                run = connection.execute(
                    """INSERT INTO quant.post_close_strategy_runs(run_key,as_of_date,model_version,status)
                       VALUES('p0-ledger-test',%s,'p0-ledger-test','completed') RETURNING run_id""",
                    (self.run_date,),
                ).fetchone()
                connection.execute(
                    """INSERT INTO quant.post_close_strategy_candidates(run_id,rank,symbol,candidate_type,score)
                       VALUES(%s,1,%s,'base_ready_30d',90),(%s,2,%s,'fresh_start_15d',70)""",
                    (run["run_id"], self.symbol_liquid, run["run_id"], self.symbol_illiquid),
                )
            with db.transaction() as connection:
                stored = materialize_post_close_candidates(connection, self.run_date)
            self.assertGreaterEqual(stored, 2)
            with db.transaction() as connection:
                rows = {row["symbol"]: dict(row) for row in connection.execute(
                    "SELECT * FROM quant.strategy_daily_candidates WHERE symbol=ANY(%s)",
                    ([self.symbol_liquid, self.symbol_illiquid],),
                ).fetchall()}
            self.assertEqual(rows[self.symbol_liquid]["strategy_key"], "post_close_base_ready")
            self.assertEqual(rows[self.symbol_liquid]["score_scale"], "bounded_0_100")
            self.assertTrue(rows[self.symbol_liquid]["liquidity_eligible"])
            self.assertEqual(rows[self.symbol_illiquid]["strategy_key"], "post_close_fresh_start")
            self.assertFalse(rows[self.symbol_illiquid]["liquidity_eligible"])
            self.assertIn("median_amount_below_floor", rows[self.symbol_illiquid]["liquidity_flags"])
        finally:
            self._cleanup()

    def test_settlement_prices_the_ledger_entry_at_next_session_open(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                _seed_bars(connection, self.symbol_liquid, self.run_date, amount=MINIMUM_MEDIAN_DAILY_AMOUNT * 2)
                run = connection.execute(
                    """INSERT INTO quant.post_close_strategy_runs(run_key,as_of_date,model_version,status)
                       VALUES('p0-ledger-test',%s,'p0-ledger-test','completed') RETURNING run_id""",
                    (self.run_date,),
                ).fetchone()
                connection.execute(
                    """INSERT INTO quant.post_close_strategy_candidates(run_id,rank,symbol,candidate_type,score)
                       VALUES(%s,1,%s,'base_ready_30d',90)""",
                    (run["run_id"], self.symbol_liquid),
                )
            with db.transaction() as connection:
                materialize_post_close_candidates(connection, self.run_date)
            with db.transaction() as connection:
                settled = settle_ledger_outcomes(connection, self.as_of_date)
            self.assertGreaterEqual(settled, 1)
            with db.transaction() as connection:
                outcome = connection.execute(
                    "SELECT * FROM quant.strategy_daily_candidate_outcomes WHERE symbol=%s", (self.symbol_liquid,)
                ).fetchone()
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["strategy_key"], "post_close_base_ready")
            self.assertEqual(Decimal(outcome["entry_price"]), Decimal("10.00"))
            self.assertEqual(outcome["horizon_days"], 10)
        finally:
            self._cleanup()


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class LimitLinkageLedgerMaterializationTests(unittest.TestCase):
    symbol = "999986.SZ"
    run_date = date(2099, 1, 2)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.strategy_daily_candidates WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.intraday_limit_linkage_candidates WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.intraday_limit_linkage_mining_runs WHERE trade_date=%s", (self.run_date,))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def test_only_the_latest_intraday_run_of_the_day_is_materialized(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                _seed_bars(connection, self.symbol, self.run_date)
                earlier = connection.execute(
                    """INSERT INTO quant.intraday_limit_linkage_mining_runs(observed_at,trade_date,status)
                       VALUES(%s,%s,'completed') RETURNING linkage_run_id""",
                    (datetime(2099, 1, 2, 2, 0, tzinfo=timezone.utc), self.run_date),
                ).fetchone()
                later = connection.execute(
                    """INSERT INTO quant.intraday_limit_linkage_mining_runs(observed_at,trade_date,status)
                       VALUES(%s,%s,'completed') RETURNING linkage_run_id""",
                    (datetime(2099, 1, 2, 6, 0, tzinfo=timezone.utc), self.run_date),
                ).fetchone()
                connection.execute(
                    """INSERT INTO quant.intraday_limit_linkage_candidates(linkage_run_id,rank,symbol,score,shared_concepts)
                       VALUES(%s,1,%s,40,1)""",
                    (earlier["linkage_run_id"], self.symbol),
                )
                connection.execute(
                    """INSERT INTO quant.intraday_limit_linkage_candidates(linkage_run_id,rank,symbol,score,shared_concepts)
                       VALUES(%s,1,%s,88,3)""",
                    (later["linkage_run_id"], self.symbol),
                )
            with db.transaction() as connection:
                stored = materialize_limit_linkage_candidates(connection, self.run_date)
            self.assertEqual(stored, 1)
            with db.transaction() as connection:
                row = connection.execute(
                    "SELECT raw_score,source_run_id FROM quant.strategy_daily_candidates WHERE symbol=%s", (self.symbol,)
                ).fetchone()
            self.assertEqual(Decimal(row["raw_score"]), Decimal("88"))
            self.assertEqual(str(row["source_run_id"]), str(later["linkage_run_id"]))
        finally:
            self._cleanup()


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class LedgerOrchestratorSmokeTests(unittest.TestCase):
    def test_materialize_ledger_runs_every_source_without_error(self) -> None:
        with db.transaction() as connection:
            result = materialize_ledger(connection, date(2099, 1, 20))
        self.assertEqual(set(result), {
            "materialize_post_close_candidates", "materialize_pattern_candidates", "materialize_ten_day_leader_candidates",
            "materialize_limit_linkage_candidates", "materialize_board_stock_mining_candidates",
            "materialize_recommendation_candidates",
        })
        self.assertTrue(all(isinstance(value, int) for value in result.values()))


if __name__ == "__main__":
    unittest.main()
