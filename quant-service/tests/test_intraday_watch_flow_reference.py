"""Real-PostgreSQL coverage for the derived-flow reference read.

The reference is what lets the licensed THS snapshot replace the public
Eastmoney watch endpoint, so the two Chinese market units it converts (万股
float share, 手 daily volume) and its point-in-time cutoff are pinned against
a real database rather than a mock.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import date, datetime, timezone

from app.async_intraday_scan_inputs_repository import watch_flow_reference
from app.main import async_db, db


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class WatchFlowReferenceIntegrationTests(unittest.TestCase):
    symbol = "999977.SZ"
    # 11:08 CST on 2026-08-26, matching the live comparison the module documents.
    observed_at = datetime(2026, 8, 26, 3, 8, tzinfo=timezone.utc)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.daily_fundamentals WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def setUp(self) -> None:
        self._cleanup()
        self.addCleanup(self._cleanup)
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO quant.instruments(symbol,exchange) VALUES(%s,'SZ') ON CONFLICT DO NOTHING",
                (self.symbol,),
            )

    def _reference(self) -> dict:
        return asyncio.run(watch_flow_reference(async_db, [self.symbol], self.observed_at))

    def _insert_bar(self, connection, trading_date: date, volume: float | None,
                    *, is_suspended: bool = False) -> None:
        connection.execute(
            """INSERT INTO quant.canonical_bars_daily(symbol,trading_date,close,volume,is_suspended,available_at,selected_provider)
               VALUES(%s,%s,10.0,%s,%s,%s,'test')""",
            (self.symbol, trading_date, volume, is_suspended, self.observed_at),
        )

    def test_units_are_converted_to_plain_shares(self) -> None:
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.daily_fundamentals(symbol,trading_date,float_share,available_at)
                   VALUES(%s,%s,%s,%s)""",
                (self.symbol, date(2026, 8, 25), 12500.0, self.observed_at),  # 万股
            )
            for day, volume in ((21, 1000.0), (24, 2000.0), (25, 3000.0)):  # 手
                self._insert_bar(connection, date(2026, 8, day), volume)
        reference = self._reference()[self.symbol]
        self.assertEqual(reference["float_shares"], 125_000_000.0, "float_share is 万股")
        self.assertEqual(reference["mean_daily_volume_shares"], 200_000.0, "volume is 手")
        self.assertEqual(reference["sample_sessions"], 3)
        self.assertEqual(reference["float_share_date"], "2026-08-25")

    def test_today_is_excluded_so_an_intraday_derivation_cannot_read_its_own_answer(self) -> None:
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO quant.daily_fundamentals(symbol,trading_date,float_share,available_at)"
                " VALUES(%s,%s,%s,%s)",
                (self.symbol, date(2026, 8, 26), 99999.0, self.observed_at),
            )
            self._insert_bar(connection, date(2026, 8, 26), 900_000.0)
            self._insert_bar(connection, date(2026, 8, 25), 1000.0)
        reference = self._reference()[self.symbol]
        self.assertIsNone(reference["float_shares"], "today's end-of-day float share must not leak in")
        self.assertEqual(reference["mean_daily_volume_shares"], 100_000.0)
        self.assertEqual(reference["sample_sessions"], 1)

    def test_only_the_five_most_recent_sessions_are_averaged(self) -> None:
        with db.transaction() as connection:
            for offset, volume in enumerate((5000.0, 4000.0, 3000.0, 2000.0, 1000.0, 999_000.0)):
                self._insert_bar(connection, date(2026, 8, 25 - offset), volume)
        reference = self._reference()[self.symbol]
        self.assertEqual(reference["sample_sessions"], 5)
        self.assertEqual(reference["mean_daily_volume_shares"], 300_000.0)

    def test_suspended_and_zero_volume_sessions_do_not_deflate_the_baseline(self) -> None:
        with db.transaction() as connection:
            self._insert_bar(connection, date(2026, 8, 25), 2000.0)
            self._insert_bar(connection, date(2026, 8, 24), 0.0)
            self._insert_bar(connection, date(2026, 8, 21), 1000.0, is_suspended=True)
        reference = self._reference()[self.symbol]
        self.assertEqual(reference["sample_sessions"], 1)
        self.assertEqual(reference["mean_daily_volume_shares"], 200_000.0)

    def test_a_symbol_with_neither_input_is_absent_rather_than_half_populated(self) -> None:
        self.assertEqual(self._reference(), {})
        self.assertEqual(asyncio.run(watch_flow_reference(async_db, [], self.observed_at)), {})


if __name__ == "__main__":
    unittest.main()
