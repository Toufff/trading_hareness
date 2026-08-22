from __future__ import annotations

import unittest

from app.analyst_market_review import ordinary_least_squares


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


if __name__ == "__main__":
    unittest.main()
