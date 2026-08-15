from __future__ import annotations

import unittest

from app.market_flow_features import (
    board_flow_breadth,
    intraday_flow_state,
    market_event_identity_key,
    volume_flow_regime,
)


class MarketFlowFeatureTests(unittest.TestCase):
    def test_board_flow_breadth_keeps_missing_separate_from_zero(self) -> None:
        rows = [
            {"taxonomy_key": "eastmoney_concept", "net_inflow": 2, "change_pct": 1},
            {"taxonomy_key": "eastmoney_concept", "net_inflow": -1, "change_pct": -0.5},
            {"taxonomy_key": "eastmoney_concept", "net_inflow": 0, "change_pct": 0.2},
            {"taxonomy_key": "eastmoney_concept", "net_inflow": None, "change_pct": 9},
            {"taxonomy_key": "eastmoney_industry", "net_inflow": 99, "change_pct": 9},
        ]
        result = board_flow_breadth(rows)
        self.assertEqual(result["board_count"], 3)
        self.assertEqual(result["positive_count"], 1)
        self.assertEqual(result["negative_count"], 1)
        self.assertEqual(result["zero_count"], 1)
        self.assertAlmostEqual(result["positive_ratio"], 1 / 3, places=6)

    def test_intraday_state_fails_closed_on_sparse_coverage(self) -> None:
        result = intraday_flow_state({"board_count": 299, "positive_ratio": 0.8, "median_flow": 3})
        self.assertEqual(result["state"], "insufficient")
        self.assertIn("concept_coverage_below_minimum", result["quality_flags"])
        self.assertTrue(result["research_only"])

    def test_intraday_state_detects_late_repair_without_calling_it_risk_on(self) -> None:
        result = intraday_flow_state(
            {"board_count": 380, "positive_ratio": 0.28, "median_flow": -5},
            five_minute_reference={"positive_ratio": 0.22},
            session_reference={"positive_ratio": 0.30},
            afternoon_min_positive_ratio=0.10,
        )
        self.assertEqual(result["state"], "late_repair")
        self.assertAlmostEqual(result["afternoon_repair_strength"], 0.18)

    def test_volume_flow_regime_detects_distribution(self) -> None:
        result = volume_flow_regime(
            {"market_amount": 256.8, "market_volume": 131.9, "advancers": 1142,
             "decliners": 4317, "unchanged": 84, "median_change_pct": -1.48},
            previous_close_summary={"market_amount": 216.7, "market_volume": 113.5},
            concept_flow={"board_count": 387, "positive_ratio": 0.059},
        )
        self.assertEqual(result["state"], "distribution")
        self.assertGreater(result["amount_change_pct"], 18)
        self.assertTrue(result["research_only"])

    def test_volume_flow_regime_detects_weak_repair(self) -> None:
        result = volume_flow_regime(
            {"market_amount": 215.7, "market_volume": 114.8, "advancers": 2400,
             "decliners": 2970, "unchanged": 173, "median_change_pct": -0.2},
            previous_close_summary={"market_amount": 256.8, "market_volume": 131.9},
            concept_flow={"board_count": 387, "positive_ratio": 0.282},
        )
        self.assertEqual(result["state"], "weak_repair")

    def test_only_mutable_pool_snapshots_receive_stable_event_identity(self) -> None:
        identity = market_event_identity_key("akshare", "limit_up_pool", "600000.SH", "2026-08-14")
        self.assertEqual(identity, "akshare:limit_up_pool:600000.SH:2026-08-14")
        self.assertIsNone(market_event_identity_key("akshare", "announcement", "600000.SH", "2026-08-14"))


if __name__ == "__main__":
    unittest.main()
