import unittest

from app.l2_research_gate import evaluate_l2_incremental_value


class L2ResearchGateTests(unittest.TestCase):
    def test_gate_fails_closed_for_small_sample(self):
        result = evaluate_l2_incremental_value([
            {"baseline_score": 0.2, "l2_score": 0.8, "outcome": 1, "l2_algorithm_version": "l2-v1"}
        ], minimum_samples=2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["live_effect"], "none")

    def test_gate_requires_positive_lower_confidence_bound(self):
        rows = [{"baseline_score": 0.0, "l2_score": 0.8, "outcome": 1, "l2_algorithm_version": "l2-v1"} for _ in range(20)]
        result = evaluate_l2_incremental_value(rows, minimum_samples=20)
        self.assertEqual(result["status"], "eligible_for_research_expansion")
        self.assertEqual(result["l2_algorithm_versions"], ["l2-v1"])


if __name__ == "__main__":
    unittest.main()
