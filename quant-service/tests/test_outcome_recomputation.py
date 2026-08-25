"""Real-PostgreSQL coverage for the next-session fillable-entry outcome guard.

A next-session entry that opens locked at the limit (or is suspended) cannot
actually be filled.  ``outcome_recomputation.recompute`` must leave such a
claim/recommendation unsettled instead of crediting or debiting a price a
real order could never have reached, and must otherwise price the fillable
entry at the next session's open (not its close).
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.main import DailyBar, db, upsert_bar
from app.outcome_recomputation import recompute as recompute_outcomes


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class ClaimOutcomeFillabilityIntegrationTests(unittest.TestCase):
    analyst_id = "p0-fillability-test-analyst"
    report_id = "p0-fillability-test-report"
    blocked_symbol = "999995.SZ"
    fillable_symbol = "999994.SZ"
    entry_date = date(2099, 1, 3)
    exit_date = date(2099, 1, 5)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.outcomes WHERE symbol=ANY(%s)", ([self.blocked_symbol, self.fillable_symbol],))
            connection.execute("DELETE FROM quant.analyst_claims WHERE remote_analyst_id=%s", (self.analyst_id,))
            connection.execute("DELETE FROM quant.analyst_evidence WHERE remote_report_id=%s", (self.report_id,))
            connection.execute("DELETE FROM quant.remote_reports WHERE remote_report_id=%s", (self.report_id,))
            connection.execute("DELETE FROM quant.remote_analysts WHERE remote_analyst_id=%s", (self.analyst_id,))
            for symbol in (self.blocked_symbol, self.fillable_symbol):
                connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (symbol,))
                connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (symbol,))

    def _seed_bars(self, connection, symbol: str, *, entry_locked: bool) -> None:
        prices = {self.entry_date: Decimal("10.00"), self.entry_date + timedelta(days=1): Decimal("10.20"),
                  self.exit_date: Decimal("10.50")}
        for trading_date, close in prices.items():
            entry_open = close * Decimal("1.10") if entry_locked and trading_date == self.entry_date else close
            upsert_bar(connection, DailyBar(
                symbol=symbol, trading_date=trading_date, open=entry_open, high=max(entry_open, close) * Decimal("1.01"),
                low=min(entry_open, close) * Decimal("0.99"), close=close, adj_factor=Decimal("1.0"),
                is_suspended=False, limit_up=close * Decimal("1.10") if trading_date == self.entry_date else None,
                limit_down=close * Decimal("0.90") if trading_date == self.entry_date else None,
                source="p0-fillability-test", available_at=datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc),
            ))

    def _seed_claim(self, connection, symbol: str) -> None:
        available_at = datetime(2099, 1, 2, 3, 0, tzinfo=timezone.utc)
        connection.execute(
            """INSERT INTO quant.remote_analysts(remote_analyst_id,name) VALUES(%s,%s)
               ON CONFLICT (remote_analyst_id) DO NOTHING""", (self.analyst_id, "P0 fillability test"),
        )
        connection.execute(
            """INSERT INTO quant.remote_reports(remote_report_id,remote_analyst_id,report_date,title,remote_version,content_hash)
               VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (remote_report_id) DO NOTHING""",
            (self.report_id, self.analyst_id, date(2099, 1, 2), "test", "v1", "c" * 64),
        )
        evidence = connection.execute(
            """INSERT INTO quant.analyst_evidence(remote_report_id,evidence_key,evidence_type,body,content_sha256,available_at)
               VALUES(%s,%s,%s,%s,%s,%s) RETURNING evidence_id""",
            (self.report_id, f"claim-{symbol}", "paragraph", "test claim", ("d" + symbol)[:64].ljust(64, "0"), available_at),
        ).fetchone()
        connection.execute(
            """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,
                      horizon_days,extraction_confidence,explicitness,extractor_version,available_at,published_at)
               VALUES(%s,%s,'stock',%s,%s,1,0.80,3,0.90,1.0,'p0-fillability-test',%s,%s)""",
            (evidence["evidence_id"], self.analyst_id, symbol, "P0 fillability test stock", available_at, available_at),
        )

    def test_locked_limit_up_open_leaves_the_claim_unsettled(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                self._seed_bars(connection, self.blocked_symbol, entry_locked=True)
                self._seed_claim(connection, self.blocked_symbol)
            recompute_outcomes(
                self.exit_date, cn_today=lambda: self.exit_date, db=db,
                recompute_intraday_signal_outcomes=lambda _as_of: {"outcome_rows": 0},
            )
            with db.transaction() as connection:
                outcome = connection.execute(
                    "SELECT * FROM quant.outcomes WHERE symbol=%s", (self.blocked_symbol,)
                ).fetchone()
            self.assertIsNone(outcome, "a locked limit-up open must not be credited as a fillable entry")
        finally:
            self._cleanup()

    def test_fillable_open_prices_the_entry_at_next_session_open(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                self._seed_bars(connection, self.fillable_symbol, entry_locked=False)
                self._seed_claim(connection, self.fillable_symbol)
            recompute_outcomes(
                self.exit_date, cn_today=lambda: self.exit_date, db=db,
                recompute_intraday_signal_outcomes=lambda _as_of: {"outcome_rows": 0},
            )
            with db.transaction() as connection:
                outcome = connection.execute(
                    "SELECT * FROM quant.outcomes WHERE symbol=%s", (self.fillable_symbol,)
                ).fetchone()
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["tradability"], "observed_open")
            self.assertEqual(Decimal(outcome["entry_close"]), Decimal("10.00"))
            self.assertEqual(Decimal(outcome["exit_close"]), Decimal("10.50"))
        finally:
            self._cleanup()


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class RecommendationOutcomeFillabilityIntegrationTests(unittest.TestCase):
    symbol = "999993.SZ"
    run_date = date(2099, 1, 2)
    entry_date = date(2099, 1, 3)
    exit_date = date(2099, 1, 5)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.outcomes WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.recommendations WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.recommendation_runs WHERE model_version=%s", ("p0-fillability-test",))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def test_locked_limit_up_open_leaves_the_recommendation_unsettled(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                for trading_date, close in ((self.entry_date, Decimal("10.00")), (self.entry_date + timedelta(days=1), Decimal("10.20")),
                                             (self.exit_date, Decimal("10.50"))):
                    locked = trading_date == self.entry_date
                    entry_open = close * Decimal("1.10") if locked else close
                    upsert_bar(connection, DailyBar(
                        symbol=self.symbol, trading_date=trading_date, open=entry_open,
                        high=max(entry_open, close) * Decimal("1.01"), low=min(entry_open, close) * Decimal("0.99"),
                        close=close, adj_factor=Decimal("1.0"), is_suspended=False,
                        limit_up=close * Decimal("1.10") if locked else None, limit_down=close * Decimal("0.90") if locked else None,
                        source="p0-fillability-test", available_at=datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc),
                    ))
                run = connection.execute(
                    """INSERT INTO quant.recommendation_runs(run_id,as_of_date,model_version,market_regime)
                       VALUES(gen_random_uuid(),%s,'p0-fillability-test','neutral') RETURNING run_id""",
                    (self.run_date,),
                ).fetchone()
                connection.execute(
                    """INSERT INTO quant.recommendations(run_id,rank,symbol,decision,score,score_breakdown,explanation,direction,horizon_days)
                       VALUES(%s,1,%s,'watch',0.5,'{}','{}',1,3)""",
                    (run["run_id"], self.symbol),
                )
            recompute_outcomes(
                self.exit_date, cn_today=lambda: self.exit_date, db=db,
                recompute_intraday_signal_outcomes=lambda _as_of: {"outcome_rows": 0},
            )
            with db.transaction() as connection:
                outcome = connection.execute(
                    "SELECT * FROM quant.outcomes WHERE symbol=%s", (self.symbol,)
                ).fetchone()
            self.assertIsNone(outcome, "a locked limit-up open must not be credited as a fillable recommendation entry")
        finally:
            self._cleanup()


if __name__ == "__main__":
    unittest.main()
