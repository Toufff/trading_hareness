from __future__ import annotations

import unittest
from pathlib import Path

from app.research_capacity import feature_readiness_projection


class FeatureReadinessProjectionTests(unittest.TestCase):
    def test_partial_enrichment_does_not_block_complete_p0_daily_baseline(self):
        result = feature_readiness_projection([
            {"feature": "daily_bars", "symbols": 1_000, "rows": 10_000},
            {"feature": "daily_basic", "symbols": 1_000, "rows": 10_000},
            {"feature": "trade_limits", "symbols": 1_000, "rows": 10_000},
            {"feature": "moneyflow", "symbols": 5, "rows": 50},
            {"feature": "announcements", "symbols": 0, "rows": 0},
        ], 1_000)
        self.assertTrue(result["decision_ready"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["supplementary_partial"], ["moneyflow", "announcements"])

    def test_missing_required_daily_input_remains_hard_blocker(self):
        result = feature_readiness_projection([
            {"feature": "daily_bars", "symbols": 1_000, "rows": 10_000},
            {"feature": "daily_basic", "symbols": 799, "rows": 10_000},
            {"feature": "trade_limits", "symbols": 1_000, "rows": 10_000},
        ], 1_000)
        self.assertFalse(result["decision_ready"])
        self.assertEqual(result["blockers"], ["daily_basic"])

    def test_cross_section_sql_requires_eighty_percent_of_the_live_universe(self):
        source = Path("app/research_capacity.py").read_text(encoding="utf-8")
        self.assertIn("greatest(ceil(universe.symbols*0.8)::int,1000)", source)
        self.assertNotIn("least(universe.symbols*0.8,1000)", source)


if __name__ == "__main__":
    unittest.main()
