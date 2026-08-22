from __future__ import annotations

import unittest

from app.dragon_leader_research import enrich_dragon_leader_watches, rank_dragon_leader_candidates


class DragonLeaderResearchTests(unittest.TestCase):
    def test_theme_ladder_is_a_manual_review_not_an_order(self) -> None:
        items = [
            {
                "ts_code": "000001.SZ", "board_count": 3, "limit_amount": 30_000_000, "status": "T字板",
                "continuation_watch": {"eligible": True, "streak_count": 3, "seal_to_float": 0.03},
                "board_context": {"exact_member_mapping": True, "sector_key": "885001.TI", "label": "测试主题", "net_amount": 100},
            },
            {
                "ts_code": "000002.SZ", "board_count": 2, "limit_amount": 20_000_000,
                "continuation_watch": {"eligible": True, "streak_count": 2, "seal_to_float": 0.02},
                "board_context": {"exact_member_mapping": True, "sector_key": "885001.TI", "label": "测试主题", "net_amount": 100},
            },
        ]

        market = enrich_dragon_leader_watches(items)
        leader = items[0]["dragon_leader_watch"]

        self.assertEqual(market["status"], "partial_post_close_limit_up_union")
        self.assertEqual(leader["leader_rank"], 1)
        self.assertEqual(leader["review_tier"], "theme_ladder_manual_review")
        self.assertEqual(leader["session_confirmation"]["status"], "not_observed")
        self.assertIn("no_automatic_order", leader["risk_flags"])

    def test_missing_theme_mapping_stays_visible_with_coverage_risk(self) -> None:
        items = [{
            "ts_code": "000003.SZ", "board_count": 2, "limit_amount": 10_000_000, "status": "一字板",
            "continuation_watch": {"eligible": True, "streak_count": 2, "seal_to_float": 0.02}, "board_context": {},
        }]

        enrich_dragon_leader_watches(items)
        leader = items[0]["dragon_leader_watch"]

        self.assertEqual(leader["theme_context"]["status"], "unavailable")
        self.assertIn("exact_theme_ladder_unavailable", leader["risk_flags"])
        self.assertIn("one_word_board_not_entry", leader["risk_flags"])
        self.assertEqual(rank_dragon_leader_candidates(items)[0]["dragon_leader_watch"]["rank"], 1)

    def test_first_board_is_filtered_even_when_the_market_context_exists(self) -> None:
        items = [{
            "ts_code": "000004.SZ", "board_count": 1,
            "continuation_watch": {"eligible": False, "streak_count": 1, "seal_to_float": 0.04}, "board_context": {},
        }]

        market = enrich_dragon_leader_watches(items)

        self.assertEqual(market["market_state"], "first_board_dominated")
        self.assertEqual(items[0]["dragon_leader_watch"]["status"], "filtered")
        self.assertEqual(rank_dragon_leader_candidates(items), [])


if __name__ == "__main__":
    unittest.main()
