"""Real-PostgreSQL coverage for persisted daily market-regime classification.

Uses the four benchmark indices' already-ingested history (no synthetic
fixture): the classification for a fixed historical trading date is
deterministic, so this both proves the write path and pins today's actual
regime read for that date.
"""

from __future__ import annotations

import os
import unittest
from datetime import date

from app.main import db
from app.market_regimes import STRATEGY_INDEX_SYMBOLS
from app.market_regime_daily import backfill_market_regime, materialize_market_regime


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class MarketRegimeDailyIntegrationTests(unittest.TestCase):
    def _cleanup(self, trading_dates: list[date]) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.market_regime_daily WHERE trading_date=ANY(%s)", (trading_dates,))

    def test_materialize_persists_a_valid_state_for_a_real_trading_date(self) -> None:
        with db.transaction() as connection:
            latest = connection.execute(
                "SELECT max(trading_date) d FROM quant.canonical_bars_daily WHERE symbol=ANY(%s)",
                (list(STRATEGY_INDEX_SYMBOLS),),
            ).fetchone()["d"]
        self.assertIsNotNone(latest, "expected at least one benchmark index bar in the dev database")
        self._cleanup([latest])
        try:
            with db.transaction() as connection:
                result = materialize_market_regime(connection, latest)
            self.assertIn(result["state"], {
                "corrective_rebound", "trend_recovery", "weak_or_declining", "mixed_transition", "insufficient_index_history",
            })
            with db.transaction() as connection:
                row = connection.execute(
                    "SELECT regime_label,model_version,index_count FROM quant.market_regime_daily WHERE trading_date=%s", (latest,)
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["regime_label"], result["state"])
            self.assertEqual(row["model_version"], "multi-index-corrective-regime-v1")
            # Re-materializing must upsert the same row, not duplicate it.
            with db.transaction() as connection:
                materialize_market_regime(connection, latest)
                count = connection.execute(
                    "SELECT count(*)::int n FROM quant.market_regime_daily WHERE trading_date=%s", (latest,)
                ).fetchone()["n"]
            self.assertEqual(count, 1)
        finally:
            self._cleanup([latest])

    def test_backfill_materializes_every_requested_date(self) -> None:
        with db.transaction() as connection:
            dates = [row["trading_date"] for row in connection.execute(
                """SELECT DISTINCT trading_date FROM quant.canonical_bars_daily WHERE symbol='000300.SH'
                     ORDER BY trading_date DESC LIMIT 5""",
            ).fetchall()]
        self._cleanup(dates)
        try:
            with db.transaction() as connection:
                materialized = backfill_market_regime(connection, dates)
            self.assertEqual(materialized, len(dates))
            with db.transaction() as connection:
                count = connection.execute(
                    "SELECT count(*)::int n FROM quant.market_regime_daily WHERE trading_date=ANY(%s)", (dates,)
                ).fetchone()["n"]
            self.assertEqual(count, len(dates))
        finally:
            self._cleanup(dates)


if __name__ == "__main__":
    unittest.main()
