from __future__ import annotations

import unittest

from app.sector_flow_features import sector_flow_feature, sector_flow_outcome


class SectorFlowFeatureTests(unittest.TestCase):
    def test_detects_flow_reversal_and_lhb_sell_pressure(self):
        result = sector_flow_feature(
            {"net_amount": 80, "change_pct": -0.5},
            previous={"net_amount": -20}, prior={"net_amount": -10},
            rank_percentile=0.9, sign_streak=1,
            lhb={"stock_count": 4, "negative_count": 3, "net_amount": -40, "limit_up_count": 2},
        )
        self.assertEqual(result["transition"], "reversal_in")
        self.assertEqual(result["price_flow_divergence"], "price_down_flow_in")
        self.assertEqual(result["lhb_sell_pressure_ratio"], 0.75)
        self.assertEqual(result["limit_up_count"], 2)
        self.assertEqual(result["live_strategy_effect"], "none")

    def test_detects_distribution_transition(self):
        result = sector_flow_feature(
            {"net_amount": -50, "change_pct": 1.2},
            previous={"net_amount": 25}, prior=None,
            rank_percentile=0.1, sign_streak=-1,
        )
        self.assertEqual(result["transition"], "reversal_out")
        self.assertEqual(result["price_flow_divergence"], "price_up_flow_out")
        self.assertIsNone(result["net_acceleration"])

    def test_outcome_is_directional_and_cross_section_adjusted(self):
        result = sector_flow_outcome(
            "reversal_out", 100, 95, cross_section_median_return=-0.02,
        )
        self.assertEqual(result["status"], "matured")
        self.assertAlmostEqual(result["raw_return"], -0.05)
        self.assertAlmostEqual(result["excess_return"], -0.03)
        self.assertAlmostEqual(result["directional_return"], 0.05)

    def test_outcome_fails_closed_for_missing_price(self):
        result = sector_flow_outcome("reversal_in", None, 101)
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
