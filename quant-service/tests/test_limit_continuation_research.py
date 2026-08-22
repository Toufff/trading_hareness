from __future__ import annotations

import unittest

from app.limit_continuation_research import continuation_watch, rank_continuation_candidates
from app.post_close_limit_features import board_count


def number(value):
    return float(value) if value is not None else None


class LimitContinuationResearchTests(unittest.TestCase):
    def test_multiboard_with_final_seal_strength_is_a_next_session_watch(self):
        result = continuation_watch({
            "ts_code": "000001.SZ", "tag": "3天2板", "limit_amount": 30_000_000,
            "free_float": 1_000_000_000, "sources": ["tushare_limit_list_ths"],
            "turnover_rate": 12, "open_num": 1,
        }, number=number, board_count=board_count)

        self.assertTrue(result["eligible"])
        self.assertEqual(result["signal_key"], "000001.SZ:watch:limit-continuation-v1")
        self.assertEqual(result["seal_to_float"], 0.03)
        self.assertIn("post_close_only", result["risk_flags"])
        self.assertIn("no_automatic_order", result["risk_flags"])

    def test_first_board_or_missing_final_seal_does_not_pass(self):
        first_board = continuation_watch({
            "ts_code": "000001.SZ", "tag": "首板", "limit_amount": 30_000_000,
            "free_float": 1_000_000_000, "sources": ["tushare_limit_list_ths"],
        }, number=number, board_count=board_count)
        missing = continuation_watch({
            "ts_code": "000002.SZ", "tag": "2天2板", "limit_amount": None,
            "free_float": 1_000_000_000, "sources": ["tushare_limit_list_ths"],
        }, number=number, board_count=board_count)

        self.assertFalse(first_board["eligible"])
        self.assertFalse(missing["eligible"])
        self.assertEqual(missing["status"], "unavailable")

    def test_ranking_is_by_seal_strength_then_streak(self):
        ranked = rank_continuation_candidates([
            {"ts_code": "000002.SZ", "continuation_watch": {"eligible": True, "seal_to_float": 0.02, "streak_count": 3}},
            {"ts_code": "000001.SZ", "continuation_watch": {"eligible": True, "seal_to_float": 0.03, "streak_count": 2}},
        ])
        self.assertEqual([item["ts_code"] for item in ranked], ["000001.SZ", "000002.SZ"])
        self.assertEqual([item["continuation_watch"]["rank"] for item in ranked], [1, 2])


if __name__ == "__main__":
    unittest.main()
