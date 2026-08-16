from __future__ import annotations

import unittest
from pathlib import Path


class MarketEventJsonMigrationTests(unittest.TestCase):
    def test_upgrade_normalizes_all_non_finite_json_literals(self):
        source = (
            Path(__file__).parents[1]
            / "migrations" / "versions" / "20260816_0036_sanitize_market_event_json.py"
        ).read_text(encoding="utf-8")
        self.assertIn("NaN", source)
        self.assertIn("Infinity", source)
        self.assertIn("body IS NOT JSON", source)
        self.assertIn("null", source)


if __name__ == "__main__":
    unittest.main()
