"""Golden-value coverage for the shared strategy threshold constants.

These constants replaced 12 inlined ``6.5``/``1.0``/``3.0`` literals that were
duplicated across the live watchlist confirmation rule, the EAC breakout
research rules, the post-close limit-lift pattern scoring and the daily-prior
shadow confirmation rules.  A change to a value here is a live-threshold
change and must go through the promotion review AGENTS.md requires, so this
test pins the values and confirms every known consumer still imports from
this single module instead of re-inlining its own copy.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.strategy_thresholds import (
    MAX_ENTRY_INTRADAY_GAIN_PCT,
    STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT,
    STANDARD_MINUTE_VOLUME_MULTIPLE_FLOOR,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"
CONSUMERS = (
    "intraday_signal_rules.py",
    "intraday_breakout.py",
    "intraday_limit_lift.py",
    "watchlist_main_wave.py",
    "watchlist_main_wave_v2.py",
    "watchlist_countertrend_rebound.py",
)


class StrategyThresholdGoldenValueTests(unittest.TestCase):
    def test_golden_values_are_unchanged(self) -> None:
        self.assertEqual(MAX_ENTRY_INTRADAY_GAIN_PCT, 6.5)
        self.assertEqual(STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT, 1.0)
        self.assertEqual(STANDARD_MINUTE_VOLUME_MULTIPLE_FLOOR, 3.0)

    def test_every_known_consumer_imports_the_shared_module(self) -> None:
        for filename in CONSUMERS:
            source = (APP_DIR / filename).read_text(encoding="utf-8")
            self.assertIn("from .strategy_thresholds import", source, filename)

    def test_no_consumer_re_inlines_the_shared_ceiling(self) -> None:
        # A bare "6.5" bound would silently drift from MAX_ENTRY_INTRADAY_GAIN_PCT.
        pattern = re.compile(r"<=\s*6\.5\b|6\.5\s*<=")
        for filename in CONSUMERS:
            source = (APP_DIR / filename).read_text(encoding="utf-8")
            self.assertNotRegex(source, pattern, filename)


if __name__ == "__main__":
    unittest.main()
