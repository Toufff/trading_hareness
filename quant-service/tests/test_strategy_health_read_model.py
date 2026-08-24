from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from app.replay_readiness import historical_replay_readiness
from app.strategy_health_read_model import strategy_health_payload_from_rows


class StrategyHealthReadModelTests(unittest.TestCase):
    def _payload(self, *, now: datetime, counts: dict | None = None,
                 outcomes: dict | None = None, latest_quotes: dict | None = None) -> dict:
        return strategy_health_payload_from_rows(
            counts or {
                "signals_7d": 12,
                "signals_prior_7d": 10,
                "episodes_7d": 4,
                "episodes_prior_7d": 4,
                "matured_30m_7d": 8,
                "matured_days_7d": 3,
                "matured_30m_total": 8,
                "matured_days_total": 3,
            },
            outcomes or {"rows": 8, "positive": 5, "avg_return": 0.003},
            latest_quotes or {
                "latest_quote_at": datetime(2026, 8, 24, 3, 29, 59, tzinfo=timezone.utc),
                "fresh_quote_rows": 0,
                "calendar_is_open": True,
            },
            [],
            now=now,
        )

    def test_lunch_break_does_not_treat_expected_quote_idle_as_an_outage(self) -> None:
        payload = self._payload(now=datetime(2026, 8, 24, 4, 28, tzinfo=timezone.utc))

        self.assertEqual(payload["market_session"]["status"], "lunch_break")
        self.assertFalse(payload["market_session"]["quote_required"])
        self.assertEqual(payload["data_freshness"]["status"], "expected_idle")
        self.assertNotEqual(payload["governance_recommendation"]["action"], "freeze_new_entries")

    def test_stale_quote_during_continuous_auction_keeps_freeze_recommendation(self) -> None:
        payload = self._payload(
            now=datetime(2026, 8, 24, 2, 10, tzinfo=timezone.utc),
            latest_quotes={
                "latest_quote_at": datetime(2026, 8, 24, 1, 29, 59, tzinfo=timezone.utc),
                "fresh_quote_rows": 0,
                "calendar_is_open": True,
            },
        )

        self.assertEqual(payload["market_session"]["status"], "continuous_auction")
        self.assertEqual(payload["data_freshness"]["status"], "stale_or_missing")
        self.assertEqual(payload["governance_recommendation"]["action"], "freeze_new_entries")

    def test_formal_validation_uses_lifetime_evidence_not_the_seven_day_health_window(self) -> None:
        payload = self._payload(
            now=datetime(2026, 8, 24, 2, 10, tzinfo=timezone.utc),
            counts={
                "signals_7d": 12,
                "signals_prior_7d": 10,
                "episodes_7d": 4,
                "episodes_prior_7d": 4,
                "matured_30m_7d": 8,
                "matured_days_7d": 3,
                "matured_30m_total": 200,
                "matured_days_total": 60,
            },
            latest_quotes={
                "latest_quote_at": datetime(2026, 8, 24, 2, 9, 30, tzinfo=timezone.utc),
                "fresh_quote_rows": 2,
                "calendar_is_open": True,
            },
        )

        self.assertEqual(payload["outcomes_30m"]["matured"], 8)
        self.assertEqual(payload["outcomes_30m"]["window_days"], 7)
        self.assertEqual(payload["validation_gate"]["observed_matured_signals"], 200)
        self.assertEqual(payload["validation_gate"]["observed_trading_days"], 60)
        self.assertEqual(payload["validation_gate"]["status"], "ready_for_formal_validation")

    def test_episode_rate_is_the_primary_drift_metric_and_raw_signal_rate_remains_diagnostic(self) -> None:
        payload = self._payload(
            now=datetime(2026, 8, 24, 2, 10, tzinfo=timezone.utc),
            counts={
                "signals_7d": 1_000,
                "signals_prior_7d": 10,
                "episodes_7d": 2,
                "episodes_prior_7d": 2,
                "matured_30m_7d": 0,
                "matured_days_7d": 0,
                "matured_30m_total": 0,
                "matured_days_total": 0,
            },
            latest_quotes={
                "latest_quote_at": datetime(2026, 8, 24, 2, 9, 30, tzinfo=timezone.utc),
                "fresh_quote_rows": 2,
                "calendar_is_open": True,
            },
        )

        frequency = payload["trigger_frequency"]
        self.assertEqual(frequency["drift_basis"], "episodes")
        self.assertEqual(frequency["drift_status"], "stable")
        self.assertEqual(frequency["raw_signal_drift_status"], "warning")


class ReplayReadinessRepositoryTests(unittest.TestCase):
    def test_full_cross_section_uses_point_in_time_universe_and_daily_controls(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {
            "full_cross_section_days": 0,
            "offline_minute_trading_days": 0,
            "offline_minute_symbols": 0,
            "offline_minute_bars": 0,
            "completed_offline_imports": 0,
            "confirmed_signal_events": 0,
            "matured_signal_events": 0,
        }
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection

        payload = historical_replay_readiness(database)
        sql = connection.execute.call_args.args[0]

        self.assertIn("universe_membership_history", sql)
        self.assertIn("daily_fundamentals", sql)
        self.assertIn("daily_trade_limits", sql)
        self.assertEqual(
            payload["coverage_definition"],
            "point_in_time_all_a_membership_with_daily_bars_fundamentals_and_trade_limits_at_80pct_min_1000",
        )


if __name__ == "__main__":
    unittest.main()
