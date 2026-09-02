from __future__ import annotations

import unittest

from app.analyst_market_review import _forward_pairs, ordinary_least_squares


class AnalystMarketReviewTests(unittest.TestCase):
    def test_regression_is_explicitly_gated(self):
        result = ordinary_least_squares([{"x": 1, "y": 2}], "x", "y")
        self.assertEqual(result["status"], "insufficient_history")
        self.assertEqual(result["live_effect"], "none")

    def test_regression_reports_coefficients_when_mature(self):
        result = ordinary_least_squares([{"x": value, "y": 2 * value + 1} for value in range(8)], "x", "y")
        self.assertEqual(result["status"], "ready")
        self.assertAlmostEqual(result["slope"], 2.0)
        self.assertAlmostEqual(result["intercept"], 1.0)
        self.assertAlmostEqual(result["r_squared"], 1.0)

    def test_regression_reports_se_and_t_stat_for_a_noisy_fit(self):
        # A deliberately imperfect fit so the residual variance (and hence
        # se_slope) is strictly positive and t_stat is defined.
        rows = [{"x": value, "y": 2 * value + 1 + (1 if value % 2 else -1)} for value in range(8)]
        result = ordinary_least_squares(rows, "x", "y")
        self.assertEqual(result["status"], "ready")
        self.assertIsNotNone(result["se_slope"])
        self.assertGreater(result["se_slope"], 0)
        self.assertIsNotNone(result["t_stat"])
        self.assertEqual(result["degrees_of_freedom"], 6)

    def test_perfect_fit_does_not_crash_on_zero_residual_variance(self):
        result = ordinary_least_squares([{"x": value, "y": 2 * value + 1} for value in range(8)], "x", "y")
        self.assertEqual(result["se_slope"], 0.0)
        # A zero standard error makes t_stat undefined (would divide by zero),
        # not infinite: reported as None rather than crashing or fabricating inf.
        self.assertIsNone(result["t_stat"])


class ForwardPairsTests(unittest.TestCase):
    def test_y_is_shifted_to_the_next_point_not_the_same_day(self):
        points = [
            {"exchange_date": "2026-08-18", "net_direction_score": 3, "market_mean_change_pct": -1.0},
            {"exchange_date": "2026-08-19", "net_direction_score": -2, "market_mean_change_pct": 1.0},
            {"exchange_date": "2026-08-20", "net_direction_score": 5, "market_mean_change_pct": 2.0},
        ]
        pairs = _forward_pairs(points, "net_direction_score", "market_mean_change_pct")
        self.assertEqual(pairs, [
            {"net_direction_score": 3, "market_mean_change_pct": 1.0},
            {"net_direction_score": -2, "market_mean_change_pct": 2.0},
        ])

    def test_missing_values_are_skipped_not_paired_with_none(self):
        points = [
            {"exchange_date": "2026-08-18", "net_direction_score": 3, "market_mean_change_pct": 4.0},
            {"exchange_date": "2026-08-19", "net_direction_score": None, "market_mean_change_pct": None},
            {"exchange_date": "2026-08-20", "net_direction_score": 7, "market_mean_change_pct": 9.0},
        ]
        # Pair (day18, day19): day19's y is None -> skipped.
        # Pair (day19, day20): day19's x is None -> skipped.
        self.assertEqual(_forward_pairs(points, "net_direction_score", "market_mean_change_pct"), [])


if __name__ == "__main__":
    unittest.main()
