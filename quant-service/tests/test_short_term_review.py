import unittest

from app.short_term_review import _limit_ratio, build_short_term_review


class LimitRatioTests(unittest.TestCase):
    def test_star_cdr_689_is_20_percent_not_mainboard_default(self):
        self.assertEqual(_limit_ratio("689009.SH"), 20.0)

    def test_mainboard_st_is_5_percent(self):
        self.assertEqual(_limit_ratio("600001.SH", is_st=True), 5.0)

    def test_star_market_st_keeps_20_percent(self):
        self.assertEqual(_limit_ratio("688009.SH", is_st=True), 20.0)


class ShortTermReviewTests(unittest.TestCase):
    def test_seven_step_projection_keeps_missing_board_membership_explicit(self):
        events = [
            {"event_type": "limit_up_pool", "symbol": "000001.SZ", "body": '{"名称":"A","连板数":2,"涨跌幅":10}'},
            {"event_type": "limit_down_pool", "symbol": "000002.SZ", "body": '{"名称":"B","涨跌幅":-10}'},
            {"event_type": "previous_limit_pool", "symbol": "000003.SZ", "body": '{"名称":"C","涨跌幅":3.2,"昨日连板数":1}'},
            {"event_type": "previous_limit_pool", "symbol": "000004.SZ", "body": '{"名称":"D","涨跌幅":-6.2,"昨日连板数":2}'},
            {"event_type": "limit_open_pool", "symbol": "000005.SZ", "body": '{"名称":"E"}'},
            {"event_type": "lhb_event", "symbol": "000001.SZ", "body": '{"龙虎榜净买额": 1200000}'},
        ]
        result = build_short_term_review(
            event_rows=events,
            daily_rows=[
                {"symbol": "000001.SZ", "name": "A", "amount": 100, "pct_chg": 10, "pre_close": 10, "high": 11, "close": 11},
                {"symbol": "000002.SZ", "name": "B", "amount": 90, "pct_chg": -10, "pre_close": 10, "high": 10.6, "close": 9},
            ],
            board_summary={"ths_concept_flow": {"inflow": [{"label": "板块A", "net_inflow": 5, "top_stocks": [], "mapped_members": 0, "quoted_members": 0}], "outflow": []}},
            observed_at="2026-08-21T08:00:00+00:00",
            tushare_lhb_context={"000001.SZ": {"institution_records": 2, "institution_net_buy": 100.5}},
        )
        self.assertEqual(result["methodology"], "short-term-review-v2")
        self.assertEqual(result["market_emotion"]["limit_up_count"], 1)
        self.assertEqual(result["market_emotion"]["previous_limit_positive_count"], 1)
        self.assertEqual(result["ladder"]["highest_board_count"], 2)
        self.assertEqual(result["capital_and_lhb"]["lhb_positive_net_count"], 1)
        self.assertEqual(result["capital_and_lhb"]["tushare_institution_records"], 2)
        self.assertEqual(result["capital_and_lhb"]["top_amount_evidence_status"], "partial")
        self.assertIsNone(result["capital_and_lhb"]["top20_amount_share"])
        self.assertEqual(result["capital_and_lhb"]["top_amount_quality_flags"], ["insufficient_all_a_daily_coverage"])
        self.assertEqual(result["sector_structure"]["candidate_mainlines"], [])
        self.assertEqual(result["loss_effect"]["previous_limit_deep_loss_count"], 1)
        self.assertEqual(result["loss_effect"]["limit_open_count"], 1)
        self.assertTrue(result["next_session_plan"]["symbol_plans"])
        self.assertIn("next_session_trigger", result["next_session_plan"]["symbol_plans"][0])
        self.assertFalse(result["next_session_plan"]["decision_eligible"])

    def test_amount_concentration_fails_closed_when_distribution_is_implausible(self):
        rows = [
            {"symbol": f"{index:06d}.SZ", "amount": 1, "pct_chg": 0}
            for index in range(3000)
        ]
        for row in rows[:20]:
            row["amount"] = 1000
        result = build_short_term_review(event_rows=[], daily_rows=rows, board_summary={})
        capital = result["capital_and_lhb"]
        self.assertEqual(capital["daily_symbol_count"], 3000)
        self.assertEqual(capital["top_amount_evidence_status"], "partial")
        self.assertEqual(capital["top_amount_quality_flags"], ["amount_distribution_anomaly"])
        self.assertIsNone(capital["top20_amount_share"])


if __name__ == "__main__":
    unittest.main()
