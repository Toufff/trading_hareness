import unittest

from app.xiaojie_leader_flow import MODEL_VERSION, evaluate_snapshot


class XiaojieLeaderFlowTests(unittest.TestCase):
    def _snapshot(self, **overrides):
        snapshot = {
            "index_above_support": True,
            "index_volume_ratio": 1.25,
            "breadth_up_count": 3200,
            "breadth_down_count": 1200,
            "main_sector_present": True,
            "sector_strength_percentile": 0.92,
            "candidate_strength_rank": 1,
            "is_back_row": False,
            "turnover_rate": 8.5,
            "volume_ratio": 1.6,
            "prior_one_word_board": True,
            "limit_up_return_flow": True,
            "re_seal_confirmed": True,
            "intraday_above_vwap": True,
            "profit_cushion_pct": 0.08,
        }
        snapshot.update(overrides)
        return snapshot

    def test_one_word_return_flow_is_a_high_risk_research_candidate(self):
        result = evaluate_snapshot(self._snapshot())
        self.assertEqual(result["model_version"], MODEL_VERSION)
        self.assertEqual(result["mode"], "one_word_return_flow")
        self.assertEqual(result["decision"], "research_candidate")
        self.assertEqual(result["position"]["target_fraction"], 0.05)
        self.assertIn("high_risk_mode", result["risk_flags"])
        self.assertEqual(result["live_effect"], "none")

    def test_missing_market_evidence_fails_closed(self):
        result = evaluate_snapshot(self._snapshot(breadth_up_count=None))
        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("insufficient_market_evidence", result["risk_flags"])

    def test_back_row_is_not_chased_even_when_market_is_good(self):
        result = evaluate_snapshot(self._snapshot(is_back_row=True, prior_one_word_board=False))
        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("back_row_no_chase", result["risk_flags"])

    def test_ma5_break_without_recovery_reduces_half(self):
        result = evaluate_snapshot(self._snapshot(
            prior_one_word_board=False,
            limit_up_return_flow=False,
            reverse_wrap_confirmed=True,
            ma5_break_duration_minutes=20,
            ma5_recovered=False,
        ))
        self.assertEqual(result["exit"]["action"], "reduce_half")
        self.assertIn("ma5_break_unrecovered", result["exit"]["codes"])

    def test_futures_and_stock_both_rising_blocks_chase(self):
        result = evaluate_snapshot(self._snapshot(futures_stock_both_rising=True))
        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("cross_asset_chase_risk", result["risk_flags"])


if __name__ == "__main__":
    unittest.main()
