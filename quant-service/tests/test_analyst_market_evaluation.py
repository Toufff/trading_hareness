from datetime import date, datetime, timezone
import unittest

from app.analyst_market_evaluation import summarize_evaluation


class AnalystMarketEvaluationTests(unittest.TestCase):
    def test_alignment_and_gate_are_research_only_until_mature(self):
        result = summarize_evaluation(
            observations=[
                {"analyst_id": "a", "strategy_available_at": datetime(2026, 8, 18, tzinfo=timezone.utc), "scope": "market", "status": "eligible", "direction": 1},
                {"analyst_id": "a", "strategy_available_at": datetime(2026, 8, 18, 1, tzinfo=timezone.utc), "scope": "stock", "status": "replay_only", "direction": -1},
            ],
            opinions=[{"remote_analyst_id": "a", "opinion_date": date(2026, 8, 18), "scope": "market", "direction": 1}],
            outcomes=[{"remote_analyst_id": "a", "status": "pending", "directional_return": None, "horizon_days": 1}],
            intraday_outcomes=[{"horizon_minutes": 5, "status": "pending", "directional_return": None}],
            market_days=[{"exchange_date": date(2026, 8, 18), "market_state": "flow_expansion", "status": "ready", "concept_positive_ratio": 0.8, "market_amount": 100}],
            sector_days=[{"sector_key": "pcb", "label": "PCB", "net_amount": 12, "lhb_negative_count": 0}],
            start_date=date(2026, 8, 18),
            end_date=date(2026, 8, 18),
        )
        self.assertEqual(result["quality_gate"]["status"], "accumulating")
        self.assertEqual(result["timeline"][0]["aligned_claims"], 1)
        self.assertEqual(result["analysts"][0]["matured_outcomes"], 0)
        self.assertEqual(result["quality_gate"]["live_strategy_effect"], "none")

    def test_contrarian_market_claim_is_visible(self):
        result = summarize_evaluation(
            observations=[],
            opinions=[{"remote_analyst_id": "a", "opinion_date": date(2026, 8, 19), "scope": "market", "direction": 1}],
            outcomes=[],
            intraday_outcomes=[],
            market_days=[{"exchange_date": date(2026, 8, 19), "market_state": "flow_risk_off", "status": "ready"}],
            sector_days=[], start_date=date(2026, 8, 19), end_date=date(2026, 8, 19),
        )
        self.assertEqual(result["timeline"][0]["contrarian_claims"], 1)

    def test_sector_baseline_uses_approved_theme_mapping(self):
        result = summarize_evaluation(
            observations=[],
            opinions=[],
            outcomes=[{
                "remote_analyst_id": "a", "opinion_id": "o1", "opinion_date": date(2026, 8, 19),
                "scope": "theme", "subject_key": "remote:pcb", "status": "matured",
                "directional_return": 0.02, "residual_return": 0.01, "horizon_days": 5,
            }],
            intraday_outcomes=[],
            market_days=[{"exchange_date": date(2026, 8, 19), "market_state": "flow_expansion"}],
            sector_days=[{"sector_key": "885959.TI", "label": "PCB", "trading_date": date(2026, 8, 19), "net_amount": 12}],
            theme_board_map={"remote:pcb": "885959.TI"},
            start_date=date(2026, 8, 19), end_date=date(2026, 8, 19),
        )
        self.assertEqual(result["baselines"]["sector_flow"]["observations"], 1)

    def test_coverage_matrix_keeps_replay_and_unavailable_separate(self):
        result = summarize_evaluation(
            observations=[
                {"analyst_id": "a", "scope": "theme", "status": "eligible", "direction": 1},
                {"analyst_id": "a", "scope": "theme", "status": "replay_only", "direction": 1},
                {"analyst_id": "a", "scope": "theme", "status": "unmapped", "direction": 1},
            ],
            opinions=[{"remote_analyst_id": "a", "opinion_id": "o1", "opinion_date": date(2026, 8, 18), "scope": "theme", "direction": 1}],
            outcomes=[
                {"remote_analyst_id": "a", "opinion_id": "o1", "scope": "theme", "status": "pending", "horizon_days": 5},
                {"remote_analyst_id": "a", "opinion_id": "o1", "scope": "theme", "status": "unavailable", "horizon_days": 5},
            ],
            intraday_outcomes=[], market_days=[], sector_days=[],
            start_date=date(2026, 8, 18), end_date=date(2026, 8, 18),
        )
        cohort = result["coverage_matrix"][0]
        self.assertEqual(cohort["observations"], 3)
        self.assertEqual(cohort["replay_only_observations"], 1)
        self.assertEqual(cohort["unmapped_observations"], 1)
        self.assertEqual(cohort["pending_outcomes"], 1)
        self.assertEqual(cohort["unavailable_outcomes"], 1)
        self.assertEqual(result["horizon_matrix"][0]["horizon_days"], 5)

    def test_mature_intraday_outcome_is_manual_evidence_and_counts_once_per_event(self):
        result = summarize_evaluation(
            observations=[{"analyst_id": "a", "scope": "stock", "status": "eligible", "direction": 1}],
            opinions=[], outcomes=[],
            intraday_outcomes=[
                {"observation_id": "obs-1", "analyst_id": "a", "horizon_minutes": 5, "status": "matured", "directional_return": 0.01},
                {"observation_id": "obs-1", "analyst_id": "a", "horizon_minutes": 15, "status": "matured", "directional_return": -0.01},
            ],
            market_days=[{"exchange_date": date(2026, 8, 18), "market_state": "mixed_rotation"}],
            sector_days=[], start_date=date(2026, 8, 18), end_date=date(2026, 8, 18),
        )
        analyst = result["analysts"][0]
        self.assertEqual(analyst["intraday_matured_events"], 1)
        self.assertEqual(analyst["intraday_matured_outcomes"], 2)
        self.assertEqual(analyst["manual_review_status"], "matured_intraday_available")
        self.assertEqual(result["quality_gate"]["matured_intraday_independent_events"], 1)
        self.assertEqual(result["quality_gate"]["matured_independent_events"], 1)
        self.assertEqual(result["quality_gate"]["live_strategy_effect"], "none")


if __name__ == "__main__":
    unittest.main()
