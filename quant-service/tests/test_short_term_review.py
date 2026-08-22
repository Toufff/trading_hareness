import unittest

from app.short_term_review import build_short_term_review


class ShortTermReviewTests(unittest.TestCase):
    def test_seven_step_projection_keeps_missing_board_membership_explicit(self):
        events = [
            {"event_type": "limit_up_pool", "symbol": "000001.SZ", "body": '{"名称":"A","连板数":2,"涨跌幅":10}'},
            {"event_type": "limit_down_pool", "symbol": "000002.SZ", "body": '{"名称":"B","涨跌幅":-10}'},
            {"event_type": "previous_limit_pool", "symbol": "000003.SZ", "body": '{"名称":"C","涨跌幅":3.2,"昨日连板数":1}'},
            {"event_type": "lhb_event", "symbol": "000001.SZ", "body": '{"龙虎榜净买额": 1200000}'},
        ]
        result = build_short_term_review(
            event_rows=events,
            daily_rows=[
                {"symbol": "000001.SZ", "name": "A", "amount": 100, "pct_chg": 10},
                {"symbol": "000002.SZ", "name": "B", "amount": 90, "pct_chg": -10},
            ],
            board_summary={"ths_concept_flow": {"inflow": [{"label": "板块A", "net_inflow": 5, "top_stocks": [], "mapped_members": 0, "quoted_members": 0}], "outflow": []}},
            observed_at="2026-08-21T08:00:00+00:00",
        )
        self.assertEqual(result["methodology"], "short-term-review-v1")
        self.assertEqual(result["market_emotion"]["limit_up_count"], 1)
        self.assertEqual(result["market_emotion"]["previous_limit_positive_count"], 1)
        self.assertEqual(result["ladder"]["highest_board_count"], 2)
        self.assertEqual(result["capital_and_lhb"]["lhb_positive_net_count"], 1)
        self.assertEqual(result["sector_structure"]["candidate_mainlines"], [])
        self.assertFalse(result["next_session_plan"]["decision_eligible"])


if __name__ == "__main__":
    unittest.main()
