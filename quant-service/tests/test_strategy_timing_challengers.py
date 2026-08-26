"""Real-PostgreSQL coverage for the offline entry-timing challenger backtest."""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta, timezone

from app.intraday_rule_inputs import intraday_rule_input_hash
from app.main import db
from app.strategy_timing_challengers import CHALLENGERS, run_challenger_backtest
from psycopg.types.json import Json


def _payload(price: float, pct_change: float = 2.0) -> dict:
    return {
        "schema_version": "intraday-rule-input-v1", "model_version": "p0-challenger-test",
        "watch": {"symbol": "999976.SZ", "entry_price": None, "available_quantity": 0,
                  "alert_on_entry": True, "alert_on_exit": True},
        "quote": {"price": price, "pct_change": pct_change, "volume_ratio": 2.0, "turnover_rate": 4.0,
                  "main_net_inflow": 100, "main_flow_percentile": 0.95},
        "previous_quote": {"price": price - 0.1},
        "daily_factors": {}, "minute_features": {}, "peer_context": {},
    }


def _evaluate_variant(inputs, overrides):
    from app.intraday_breakout import upside_research_assessment
    from app.intraday_signal_rules import signal_rules

    def number(value):
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return signal_rules(
        inputs["watch"], inputs["quote"], inputs["previous_quote"], inputs["daily_factors"],
        inputs["minute_features"], inputs["peer_context"], number=number,
        upside_assessment_fn=lambda q, d, m, p: upside_research_assessment(q, d, m, p, number=number, eac_window=True),
        model_version="p0-challenger-test", **overrides,
    )


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class StrategyTimingChallengerIntegrationTests(unittest.TestCase):
    symbol = "999976.SZ"
    as_of_date = date(2099, 1, 20)
    model_version = "p0-challenger-test"

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.intraday_rule_input_snapshots WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.intraday_scan_runs WHERE observed_at::date=%s", (self.as_of_date,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))
            connection.execute(
                "DELETE FROM quant.strategy_experiments WHERE strategy_key=%s AND start_date=%s",
                ("intraday_entry_timing_challengers_v1", self.as_of_date),
            )

    def _insert_snapshot(self, connection, observed_at: datetime, price: float, pct_change: float = 2.0) -> None:
        scan = connection.execute(
            "INSERT INTO quant.intraday_scan_runs(observed_at,status) VALUES(%s,'completed') RETURNING scan_id",
            (observed_at,),
        ).fetchone()
        payload = _payload(price, pct_change)
        connection.execute(
            """INSERT INTO quant.intraday_rule_input_snapshots(scan_id,symbol,observed_at,model_version,input_hash,inputs)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (scan["scan_id"], self.symbol, observed_at, self.model_version, intraday_rule_input_hash(payload), Json(payload)),
        )

    def test_c1_tighter_ceiling_fires_fewer_entries_than_baseline_with_a_measurable_forward_return(self) -> None:
        self._cleanup()
        try:
            base_time = datetime(2099, 1, 20, 2, 0, tzinfo=timezone.utc)
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO quant.instruments(symbol,exchange) VALUES(%s,'SZ') ON CONFLICT DO NOTHING", (self.symbol,),
                )
                # Trigger snapshot: pct_change=4.0, above a 3.0 challenger ceiling but inside the live 1-6.5 band.
                self._insert_snapshot(connection, base_time, price=10.4, pct_change=4.0)
                # Follow-up snapshots for this same symbol reconstruct its forward price path.
                self._insert_snapshot(connection, base_time + timedelta(minutes=5), price=10.6, pct_change=6.0)
                self._insert_snapshot(connection, base_time + timedelta(minutes=15), price=10.8, pct_change=8.0)
                self._insert_snapshot(connection, base_time + timedelta(minutes=30), price=11.0, pct_change=10.0)
            with db.transaction() as connection:
                result = run_challenger_backtest(
                    connection, self.as_of_date, model_version=self.model_version, evaluate_variant=_evaluate_variant,
                )
            self.assertEqual(result["status"], "completed")
            baseline = result["challengers"]["baseline"]
            c1 = result["challengers"]["c1_tighter_entry_ceiling_3pct"]
            self.assertGreaterEqual(baseline["total_entries"], 1)
            self.assertEqual(c1["total_entries"], 0, "pct_change=4.0 must not clear a 3.0 ceiling")
            self.assertIsNotNone(baseline["by_horizon"]["5m"]["avg_return"])
            self.assertAlmostEqual(baseline["by_horizon"]["5m"]["avg_return"], 10.6 / 10.4 - 1, places=5)
        finally:
            self._cleanup()

    def test_blocked_status_for_a_date_with_no_recorded_snapshots(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                result = run_challenger_backtest(
                    connection, date(2099, 6, 1), model_version=self.model_version, evaluate_variant=_evaluate_variant,
                )
            self.assertEqual(result["status"], "blocked")
        finally:
            self._cleanup()

    def test_challenger_keys_are_stable(self) -> None:
        self.assertEqual(set(CHALLENGERS), {
            "baseline", "c1_tighter_entry_ceiling_3pct", "c2_entry_session_windows",
            "c3_entry_requires_minute_confirmation",
        })


if __name__ == "__main__":
    unittest.main()
