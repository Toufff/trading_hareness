import unittest

from app.analysis import direction_source, extract_signals, normalize_symbol
from app.contextual_policy_learning import contextual_bandit_policy_review


class AnalysisExtractionTests(unittest.TestCase):
    def test_a_share_exchange_normalization(self):
        self.assertEqual(normalize_symbol("600519"), ("600519.SH", "SSE"))
        self.assertEqual(normalize_symbol("300750"), ("300750.SZ", "SZSE"))
        self.assertEqual(normalize_symbol("830799"), ("830799.BJ", "BSE"))
        self.assertEqual(normalize_symbol("688981"), ("688981.SH", "SSE"))
        self.assertEqual(normalize_symbol("430047"), ("430047.BJ", "BSE"))

    def test_nearby_opinions_do_not_cancel_each_other(self):
        signals = {signal.symbol: signal for signal in extract_signals("看好600519，建议中线布局；回避300750短线风险")}
        self.assertEqual(signals["600519.SH"].direction, 1)
        self.assertEqual(signals["600519.SH"].horizon_days, 20)
        self.assertEqual(signals["300750.SZ"].direction, -1)
        self.assertEqual(signals["300750.SZ"].horizon_days, 5)

    def test_unqualified_mention_is_watch_not_trade_signal(self):
        signals = extract_signals("今天只记录000001的财报发布日期")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].direction, 0)

    def test_explicit_portfolio_actions_are_directional(self):
        signals = {signal.symbol: signal for signal in extract_signals("689009 九号公司 调入；603369 今世缘 调出")}
        self.assertEqual(signals["689009.SH"].direction, 1)
        self.assertEqual(signals["603369.SH"].direction, -1)
        self.assertEqual(direction_source(signals["689009.SH"].evidence_text), "explicit_action_positive")

    def test_offline_policy_learning_keeps_small_sample_descriptive(self):
        result = contextual_bandit_policy_review([{
            "exchange_date": "2026-08-11", "signal_type": "entry", "status": "matured",
            "raw_return": 0.01, "maximum_adverse_excursion": -0.002,
            "evidence": {"attribution": {"model_version": "eac-v4", "stage": "acceptance",
                                            "market_state": "risk_on", "sector_linkage": "peer_confirmed"}},
        }], focus_exchange_date="2026-08-11")
        self.assertEqual(result["policy_update"], "disabled")
        self.assertEqual(result["validation_gate"]["status"], "accumulating")
        self.assertEqual(result["action_values"][0]["status"], "descriptive_only")
        self.assertNotIn("mean_directional_reward_bps", result["action_values"][0])

    def test_offline_policy_learning_only_exposes_values_after_full_gate(self):
        rows = []
        for index in range(200):
            rows.append({
                "exchange_date": f"2026-06-{index % 60 + 1:02d}", "signal_type": "entry", "status": "matured",
                "raw_return": 0.01, "maximum_adverse_excursion": -0.002,
                "evidence": {"attribution": {"model_version": "eac-v4", "stage": "acceptance",
                                                "market_state": "risk_on", "sector_linkage": "peer_confirmed"}},
            })
        result = contextual_bandit_policy_review(rows, focus_exchange_date="2026-06-01")
        action = result["action_values"][0]
        self.assertEqual(result["validation_gate"]["status"], "ready_for_offline_policy_review")
        self.assertEqual(action["status"], "reviewable_offline_only")
        self.assertEqual(action["mean_directional_reward_bps"], 100.0)


if __name__ == "__main__":
    unittest.main()
