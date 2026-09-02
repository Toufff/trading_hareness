from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import os
import unittest
from unittest.mock import MagicMock

from app.analyst_scorecards import readiness, recompute


class _Transaction:
    def __init__(self, execute):
        self.execute = execute

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class AnalystScorecardReadinessTests(unittest.TestCase):
    def test_readiness_preserves_maturity_gate_reasons(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"remote_analyst_id": "neutral", "directional_stock_claims": 0, "settled_stock_outcomes": 100},
            {"remote_analyst_id": "early", "directional_stock_claims": 4, "settled_stock_outcomes": 29},
            {"remote_analyst_id": "mature", "directional_stock_claims": 4, "settled_stock_outcomes": 30},
        ]

        result = readiness(connection)

        self.assertEqual(
            [(item["remote_analyst_id"], item["mature"], item["reason"]) for item in result],
            [
                ("neutral", True, "no_directional_stock_claims"),
                ("early", False, "fewer_than_30_settled_stock_outcomes"),
                ("mature", True, "eligible_for_scorecard_review"),
            ],
        )

    def test_recompute_uses_only_versioned_remote_claims(self) -> None:
        statements = []

        def execute(sql, params=()):
            statements.append((str(sql), params))
            return MagicMock(fetchall=MagicMock(return_value=[]))

        database = MagicMock()
        database.transaction.return_value = _Transaction(execute)

        result = recompute(
            date(2026, 8, 21), cn_today=lambda: date(2026, 8, 21), db=database, readiness=lambda _connection: [],
        )

        self.assertEqual(result["scorecards"], 0)
        source_sql = statements[0][0]
        self.assertIn("quant.analyst_claims", source_sql)
        self.assertNotIn("quant.analyst_signals", source_sql)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class AnalystScorecardOneDayHorizonRegressionTests(unittest.TestCase):
    """A 1-day horizon claim must not fold into a hit_rate/return of zero.

    Before this fix the scorecard priced entry and exit at the same close
    (``OFFSET (horizon_days - 1)`` from an entry keyed by close, not open),
    so every single-day claim's raw_return was identically zero regardless of
    what the stock actually did the next session.
    """

    analyst_id = "p0-scorecard-h1-analyst"
    report_id = "p0-scorecard-h1-report"
    symbol = "999991.SZ"
    entry_date = date(2099, 1, 3)
    next_date = date(2099, 1, 4)

    def _cleanup(self) -> None:
        from app.main import db
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.analyst_scorecards WHERE analyst_id=%s", (self.analyst_id,))
            connection.execute("DELETE FROM quant.outcomes WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.analyst_claims WHERE remote_analyst_id=%s", (self.analyst_id,))
            connection.execute("DELETE FROM quant.analyst_evidence WHERE remote_report_id=%s", (self.report_id,))
            connection.execute("DELETE FROM quant.remote_reports WHERE remote_report_id=%s", (self.report_id,))
            connection.execute("DELETE FROM quant.remote_analysts WHERE remote_analyst_id=%s", (self.analyst_id,))
            connection.execute("DELETE FROM quant.canonical_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.market_bars_daily WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.raw_market_observations WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))
            connection.execute(
                "DELETE FROM quant.market_trade_calendar WHERE exchange='SSE' AND calendar_date=ANY(%s)",
                ([self.entry_date, self.next_date],),
            )

    def test_one_day_horizon_hit_rate_reflects_the_next_session(self) -> None:
        from app.main import DailyBar, db, upsert_bar

        self._cleanup()
        try:
            with db.transaction() as connection:
                for trading_date in (self.entry_date, self.next_date):
                    connection.execute(
                        """INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,provider,available_at)
                           VALUES('SSE',%s,true,'p0-scorecard-h1-test',%s)
                           ON CONFLICT(exchange,calendar_date) DO UPDATE SET is_open=true""",
                        (trading_date, datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc)),
                    )
                for trading_date, open_price, close in (
                    (self.entry_date, Decimal("10.00"), Decimal("10.00")),
                    (self.next_date, Decimal("10.00"), Decimal("11.00")),
                ):
                    upsert_bar(connection, DailyBar(
                        symbol=self.symbol, trading_date=trading_date, open=open_price,
                        high=max(open_price, close) * Decimal("1.01"), low=min(open_price, close) * Decimal("0.99"),
                        close=close, adj_factor=Decimal("1.0"), is_suspended=False,
                        source="p0-scorecard-h1-test", available_at=datetime.combine(trading_date, datetime.min.time(), tzinfo=timezone.utc),
                    ))
                available_at = datetime(2099, 1, 2, 3, 0, tzinfo=timezone.utc)
                connection.execute(
                    """INSERT INTO quant.remote_analysts(remote_analyst_id,name) VALUES(%s,%s)
                       ON CONFLICT (remote_analyst_id) DO NOTHING""", (self.analyst_id, "P0 scorecard h1 test"),
                )
                connection.execute(
                    """INSERT INTO quant.remote_reports(remote_report_id,remote_analyst_id,report_date,title,remote_version,content_hash)
                       VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (remote_report_id) DO NOTHING""",
                    (self.report_id, self.analyst_id, date(2099, 1, 2), "test", "v1", "a" * 64),
                )
                evidence = connection.execute(
                    """INSERT INTO quant.analyst_evidence(remote_report_id,evidence_key,evidence_type,body,content_sha256,available_at)
                       VALUES(%s,%s,%s,%s,%s,%s) RETURNING evidence_id""",
                    (self.report_id, f"claim-{self.symbol}", "paragraph", "test claim", ("b" + self.symbol)[:64].ljust(64, "0"), available_at),
                ).fetchone()
                connection.execute(
                    """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,
                              horizon_days,extraction_confidence,explicitness,extractor_version,available_at,published_at)
                       VALUES(%s,%s,'stock',%s,%s,1,0.80,1,0.90,1.0,'p0-scorecard-h1-test',%s,%s)""",
                    (evidence["evidence_id"], self.analyst_id, self.symbol, "P0 scorecard h1 test stock", available_at, available_at),
                )
            result = recompute(self.next_date, cn_today=lambda: self.next_date, db=db, readiness=readiness)
            self.assertEqual(result["scorecards"], 1)
            with db.transaction() as connection:
                scorecard = connection.execute(
                    "SELECT * FROM quant.analyst_scorecards WHERE analyst_id=%s AND horizon_days=1", (self.analyst_id,),
                ).fetchone()
            self.assertIsNotNone(scorecard)
            self.assertEqual(scorecard["observations"], 1)
            self.assertEqual(float(scorecard["hit_rate"]), 1.0)
            self.assertNotEqual(float(scorecard["mean_directional_return"]), 0.0)
        finally:
            self._cleanup()
