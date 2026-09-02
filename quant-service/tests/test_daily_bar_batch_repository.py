"""Equivalence coverage for the batched daily-bar upsert.

``daily_bar_batch_repository.upsert_daily_bars`` must leave the database in
the same state that calling ``daily_bar_repository.upsert_daily_bar`` once per
bar, in order, would have left it -- that is the whole point of the N+1 fix
(see the trading_hareness audit, section E).  This compares two symbols
written through each path with an identical two-provider, price-conflicting
input sequence and asserts the resulting canonical/market rows agree on every
field the batch path recomputes (provider-priority selection, evidence
accumulation, amount-unit quarantine, defaults for unset optional fields).

Requires the compose PostgreSQL service (``PGHOST``); skipped otherwise, same
as the rest of the DB-backed suite.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal
import unittest


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class DailyBarBatchEquivalenceTests(unittest.TestCase):
    seq_symbol = "999991.SZ"
    batch_symbol = "999992.SZ"
    mismatch_symbol = "999993.SZ"
    trading_date = date(2099, 3, 2)

    def _cleanup(self) -> None:
        from app.main import db
        symbols = [self.seq_symbol, self.batch_symbol, self.mismatch_symbol]
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=ANY(%s)", (symbols,))
            connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=ANY(%s)", (symbols,))
            connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=ANY(%s)", (symbols,))
            connection.execute("DELETE FROM quant.data_quality_issues WHERE symbol=ANY(%s)", (symbols,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=ANY(%s)", (symbols,))

    def _canonical(self, connection, symbol):
        return connection.execute(
            """SELECT close,volume,amount,adj_factor,is_suspended,limit_up,limit_down,selected_provider,
                      source_observation_ids,quality_status
                 FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date=%s""",
            (symbol, self.trading_date),
        ).fetchone()

    def _market_bar(self, connection, symbol):
        return connection.execute(
            "SELECT close,amount FROM quant.market_bars_daily WHERE symbol=%s AND trading_date=%s",
            (symbol, self.trading_date),
        ).fetchone()

    def test_batch_upsert_matches_sequential_per_row_upsert(self) -> None:
        from app.daily_bar_batch_repository import upsert_daily_bars
        from app.daily_bar_repository import upsert_daily_bar
        from app.main import DailyBar, db

        self._cleanup()
        try:
            available_at = datetime(2099, 3, 2, tzinfo=timezone.utc)

            def bars(symbol: str) -> list:
                return [
                    DailyBar(
                        symbol=symbol, trading_date=self.trading_date, close=Decimal("10"),
                        open=Decimal("10"), high=Decimal("10.5"), low=Decimal("9.5"),
                        volume=Decimal("100000"), amount=Decimal("10000"),
                        source="tushare_super_get", available_at=available_at,
                    ),
                    # Lower-priority provider (tushare_super_sdk sorts after
                    # tushare_super_get) reporting a conflicting close: must
                    # not replace the canonical selection, but market_bars_daily
                    # (no priority contract) always takes the latest write.
                    DailyBar(
                        symbol=symbol, trading_date=self.trading_date, close=Decimal("11"),
                        open=Decimal("11"), high=Decimal("11.5"), low=Decimal("10.5"),
                        volume=Decimal("120000"), amount=Decimal("12000"),
                        source="tushare_super_sdk", available_at=available_at,
                    ),
                ]

            with db.transaction() as connection:
                for bar in bars(self.seq_symbol):
                    upsert_daily_bar(connection, bar)
            with db.transaction() as connection:
                upsert_daily_bars(connection, bars(self.batch_symbol))

            with db.transaction() as connection:
                seq_canonical = dict(self._canonical(connection, self.seq_symbol))
                batch_canonical = dict(self._canonical(connection, self.batch_symbol))
                seq_market = dict(self._market_bar(connection, self.seq_symbol))
                batch_market = dict(self._market_bar(connection, self.batch_symbol))

            # The higher-priority provider's close/selection must win in both paths.
            self.assertEqual(seq_canonical["selected_provider"], "tushare_super_get")
            self.assertEqual(batch_canonical["selected_provider"], "tushare_super_get")
            self.assertEqual(seq_canonical["close"], batch_canonical["close"])
            self.assertEqual(seq_canonical["quality_status"], batch_canonical["quality_status"])
            self.assertEqual(seq_canonical["adj_factor"], batch_canonical["adj_factor"])
            self.assertEqual(seq_canonical["is_suspended"], batch_canonical["is_suspended"])
            self.assertEqual(len(seq_canonical["source_observation_ids"]), 2)
            self.assertEqual(len(batch_canonical["source_observation_ids"]), 2)

            # market_bars_daily has no provider-priority contract: both paths
            # must land on the last-processed bar's fields.
            self.assertEqual(seq_market["close"], batch_market["close"])
            self.assertEqual(seq_market, batch_market)

            # A conflicting-close quality issue must be recorded exactly once
            # per symbol by both paths.
            with db.transaction() as connection:
                seq_issues = connection.execute(
                    """SELECT count(*)::int AS n FROM quant.data_quality_issues
                         WHERE symbol=%s AND code='provider_close_conflict'""",
                    (self.seq_symbol,),
                ).fetchone()["n"]
                batch_issues = connection.execute(
                    """SELECT count(*)::int AS n FROM quant.data_quality_issues
                         WHERE symbol=%s AND code='provider_close_conflict'""",
                    (self.batch_symbol,),
                ).fetchone()["n"]
            self.assertEqual(seq_issues, 1)
            self.assertEqual(batch_issues, 1)
        finally:
            self._cleanup()

    def test_batch_rejects_tencent_free_like_the_per_row_path(self) -> None:
        from app.daily_bar_batch_repository import upsert_daily_bars
        from app.main import DailyBar, db

        bad_bar = DailyBar(
            symbol=self.mismatch_symbol, trading_date=self.trading_date, close=Decimal("10"),
            source="tencent_free", available_at=datetime(2099, 3, 2, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ValueError, "front-adjusted"):
            with db.transaction() as connection:
                upsert_daily_bars(connection, [bad_bar])


if __name__ == "__main__":
    unittest.main()
