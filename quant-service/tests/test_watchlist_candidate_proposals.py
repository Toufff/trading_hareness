"""Real-PostgreSQL coverage for the read-only cross-strategy watchlist proposal list."""

from __future__ import annotations

import os
import unittest
from datetime import date

from app.main import db
from app.watchlist_candidate_proposals import latest_watchlist_proposals, materialize_watchlist_proposals


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class WatchlistCandidateProposalsIntegrationTests(unittest.TestCase):
    symbol_a, symbol_b, symbol_c = "999979.SZ", "999978.SZ", "999977.SZ"
    as_of_date = date(2099, 1, 20)

    def _cleanup(self) -> None:
        symbols = [self.symbol_a, self.symbol_b, self.symbol_c]
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.strategy_watchlist_proposals WHERE symbol=ANY(%s)", (symbols,))
            connection.execute("DELETE FROM quant.strategy_daily_candidates WHERE symbol=ANY(%s)", (symbols,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (symbols,))

    def _seed_instrument(self, connection, symbol: str) -> None:
        connection.execute(
            "INSERT INTO quant.instruments(symbol,exchange) VALUES(%s,'SZ') ON CONFLICT(symbol) DO NOTHING", (symbol,),
        )

    def test_ineligible_candidates_are_excluded_and_best_strategy_per_symbol_wins(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                for symbol in (self.symbol_a, self.symbol_b, self.symbol_c):
                    self._seed_instrument(connection, symbol)
                # symbol_a: two strategies score it; the higher-percentile one must win.
                connection.execute(
                    """INSERT INTO quant.strategy_daily_candidates(strategy_key,as_of_date,symbol,source_table,raw_score,score_scale,liquidity_eligible)
                       VALUES('strategy_low',%s,%s,'t',10,'bounded_0_100',true)""",
                    (self.as_of_date, self.symbol_a),
                )
                connection.execute(
                    """INSERT INTO quant.strategy_daily_candidates(strategy_key,as_of_date,symbol,source_table,raw_score,score_scale,liquidity_eligible)
                       VALUES('strategy_high',%s,%s,'t',90,'bounded_0_100',true)""",
                    (self.as_of_date, self.symbol_a),
                )
                # symbol_b: liquidity_eligible=false must never appear.
                connection.execute(
                    """INSERT INTO quant.strategy_daily_candidates(strategy_key,as_of_date,symbol,source_table,raw_score,score_scale,liquidity_eligible)
                       VALUES('strategy_high',%s,%s,'t',95,'bounded_0_100',false)""",
                    (self.as_of_date, self.symbol_b),
                )
                # symbol_c: eligible, moderate score.
                connection.execute(
                    """INSERT INTO quant.strategy_daily_candidates(strategy_key,as_of_date,symbol,source_table,raw_score,score_scale,liquidity_eligible)
                       VALUES('strategy_high',%s,%s,'t',50,'bounded_0_100',true)""",
                    (self.as_of_date, self.symbol_c),
                )
            with db.transaction() as connection:
                stored = materialize_watchlist_proposals(connection, self.as_of_date, top_k=10)
            self.assertEqual(stored['strategy_ledger'], 2)
            with db.transaction() as connection:
                rows = {row["symbol"]: dict(row) for row in connection.execute(
                    "SELECT * FROM quant.strategy_watchlist_proposals WHERE as_of_date=%s", (self.as_of_date,),
                ).fetchall()}
            self.assertNotIn(self.symbol_b, rows, "an illiquid candidate must never be proposed")
            self.assertEqual(rows[self.symbol_a]["strategy_key"], "strategy_high")
            self.assertEqual(rows[self.symbol_a]["proposal_rank"], 1)
            self.assertEqual(rows[self.symbol_c]["proposal_rank"], 2)
        finally:
            self._cleanup()

    def test_top_k_bounds_the_proposal_count(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                for symbol in (self.symbol_a, self.symbol_b, self.symbol_c):
                    self._seed_instrument(connection, symbol)
                    connection.execute(
                        """INSERT INTO quant.strategy_daily_candidates(strategy_key,as_of_date,symbol,source_table,raw_score,score_scale,liquidity_eligible)
                           VALUES('strategy_high',%s,%s,'t',50,'bounded_0_100',true)""",
                        (self.as_of_date, symbol),
                    )
            with db.transaction() as connection:
                stored = materialize_watchlist_proposals(connection, self.as_of_date, top_k=2)
            self.assertEqual(stored['strategy_ledger'], 2)
            with db.transaction() as connection:
                result = latest_watchlist_proposals(connection)
            self.assertEqual(result["as_of_date"], str(self.as_of_date))
            self.assertEqual(len(result["proposals"]), 2)
            self.assertIn("never written into quant.intraday_watchlists", result["notice"])
        finally:
            self._cleanup()

    def test_rerunning_replaces_the_prior_days_proposals(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                self._seed_instrument(connection, self.symbol_a)
                connection.execute(
                    """INSERT INTO quant.strategy_daily_candidates(strategy_key,as_of_date,symbol,source_table,raw_score,score_scale,liquidity_eligible)
                       VALUES('strategy_high',%s,%s,'t',50,'bounded_0_100',true)""",
                    (self.as_of_date, self.symbol_a),
                )
            with db.transaction() as connection:
                materialize_watchlist_proposals(connection, self.as_of_date)
                materialize_watchlist_proposals(connection, self.as_of_date)
                count = connection.execute(
                    "SELECT count(*)::int n FROM quant.strategy_watchlist_proposals WHERE as_of_date=%s", (self.as_of_date,)
                ).fetchone()["n"]
            self.assertEqual(count, 1)
        finally:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()
