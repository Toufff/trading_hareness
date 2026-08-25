from __future__ import annotations

import unittest

from app.background_task_catalog import build_specs


class BackgroundTaskCatalogTests(unittest.TestCase):
    def test_builds_declared_specs_with_injected_enablement_and_factories(self):
        labels = (
            "intraday_monitor", "super_get_fast_quote", "strategy_review", "post_close_strategy",
            "ten_day_leader_rotation", "daily_strategy_summary", "ths_member_backfill",
            "all_board_member_backfill", "minute_profile_capture", "tencent_order_book", "board_flow_curve",
        )
        loops = {label: (lambda label=label: label) for label in labels}
        specs = build_specs(interval_seconds=20, enabled={label: label == "strategy_review" for label in labels}, loops=loops)
        self.assertEqual(tuple(spec.label for spec in specs), labels)
        self.assertEqual([spec.enabled for spec in specs], [label == "strategy_review" for label in labels])
        self.assertEqual(specs[2].factory(), "strategy_review")


if __name__ == "__main__":
    unittest.main()
