from __future__ import annotations

import unittest

from app.intraday_decision_context import (
    decision_context,
    probability_for_signal,
    probability_profiles_from_rows,
    shrunk_probability,
)
from app.probability_calibration import out_of_fold_calibration_diagnostics


class IntradayDecisionContextTests(unittest.TestCase):
    def test_probability_uses_trading_days_as_effective_sample(self) -> None:
        profile = shrunk_probability(
            raw_positive_rate=1.0, sample_rows=10, independent_days=2,
            average_directional_return=0.10, horizon="5d",
            source="countertrend_rebound_diagnostic",
        )
        self.assertIsNone(profile["estimated_probability"])
        self.assertAlmostEqual(profile["historical_condition_baseline"], 12 / 22, places=4)
        self.assertEqual(profile["confidence_tier"], "uncalibrated")
        self.assertEqual(profile["sample_rows"], 10)
        self.assertLess(profile["confidence_interval_lower"], profile["historical_condition_baseline"])
        self.assertGreater(profile["confidence_interval_upper"], profile["historical_condition_baseline"])

    def test_live_profiles_collapse_correlated_rows_by_day(self) -> None:
        rows = [
            {"signal_key": f"00000{i}.SZ:entry:sector_surge_v1", "signal_type": "entry",
             "exchange_date": "2026-08-10", "raw_return": value}
            for i, value in enumerate((0.01, 0.02, -0.01, 0.03))
        ]
        profile = probability_profiles_from_rows(rows)["sector_surge:entry"]
        self.assertEqual(profile["sample_rows"], 4)
        self.assertEqual(profile["independent_trading_days"], 1)
        self.assertEqual(profile["raw_event_positive_rate"], 0.75)

    def test_preregistered_rebound_probability_takes_precedence(self) -> None:
        expected = {"estimated_probability": 0.31, "sample_rows": 10}
        signal = {"signal_key": "000001.SZ:entry:countertrend_rebound_v1", "signal_type": "entry",
                  "conditions": {"research_probability": expected}}
        profile = probability_for_signal(signal, {})
        self.assertIsNone(profile["estimated_probability"])
        self.assertAlmostEqual(profile["historical_condition_baseline"], 0.31)
        self.assertFalse(profile["display_eligible"])

    def test_only_validated_oof_profile_can_display_a_probability(self) -> None:
        expected = {
            "estimated_probability": 0.61, "sample_rows": 220,
            "independent_trading_days": 64, "calibration_status": "validated",
        }
        signal = {"signal_key": "000001.SZ:entry:countertrend_rebound_v1", "signal_type": "entry",
                  "conditions": {"research_probability": expected}}
        profile = probability_for_signal(signal, {})
        self.assertTrue(profile["display_eligible"])
        self.assertEqual(profile["estimated_probability"], 0.61)

    def test_entry_and_exit_contexts_explain_reason_and_invalidation(self) -> None:
        probability = {"estimated_probability": None, "sample_rows": 0}
        entry = decision_context({
            "signal_type": "entry", "conditions": {
                "setup": "countertrend_rebound_confirmed_plus_intraday_acceptance",
                "volume_ratio": 1.8, "main_net_inflow": 100,
                "minute_features": {"return_3m_pct": 0.8, "above_vwap_pct": 0.3,
                                     "minute_volume_multiple": 2.0},
                "peer_context": {"confirming_peer_count": 2},
            },
        }, probability)
        self.assertEqual(entry["action"], "入场复核")
        self.assertIn("B浪反弹确认态", entry["reasons"][0])
        self.assertTrue(entry["invalidations"])
        exit_context = decision_context({
            "signal_type": "exit", "conditions": {"hard_stop": 9.5},
        }, probability)
        self.assertEqual(exit_context["action"], "离场复核")
        self.assertIn("硬止损", exit_context["reasons"][0])

    def test_calibration_refuses_to_fit_below_independent_day_gate(self) -> None:
        result = out_of_fold_calibration_diagnostics([
            {"probability": 0.7, "outcome": 1, "exchange_date": "2026-08-10"},
        ])
        self.assertEqual(result["status"], "insufficient_oof_evidence")
        self.assertEqual(result["required_trading_days"], 60)


if __name__ == "__main__":
    unittest.main()
