import unittest
from datetime import date, datetime, timezone

from app.analyst_trade_actions import parse_anqiang_trade_actions
from app.analyst_skill_models import PROMPT_VARIANTS, _variant_payload
from app.remote_archive import classify_remote_text, evidence_fragments, extract_topics, horizon_days, labels, normalize_topic_key, report_topic_labels, text_hash, text_only_remote_report


class RemoteArchiveNormalizationTests(unittest.TestCase):
    def test_horizon_and_sentiment_are_deterministic(self):
        self.assertEqual(horizon_days("明日关注"), 1)
        self.assertEqual(horizon_days("中线布局"), 20)
        self.assertEqual(classify_remote_text("看好并建议加仓")[0], 1)
        self.assertEqual(classify_remote_text("回避并注意风险")[0], -1)

    def test_evidence_keeps_report_sections_without_media_processing(self):
        report = {"raw_markdown": "报告正文", "summary": "摘要", "sections": {"strategy": "策略正文"}}
        fragments = evidence_fragments(report)
        self.assertEqual([key for key, _, _ in fragments], ["raw_markdown", "summary", "section:strategy"])
        self.assertEqual(text_hash("报告正文"), text_hash("报告正文"))

    def test_text_only_ingress_strips_media_links_and_discards_materials(self):
        report = {
            "analyst": {"analyst_id": "liwei", "name": "立伟"}, "report_id": "liwei:2026-08-10",
            "date": "2026-08-10", "version": "v1", "content_hash": "sha256:test",
            "raw_markdown": "![图](https://remote.example/chart.jpg)\n[完整原文](https://remote.example/a.md)\n看好半导体",
            "sections": {"market_view": "视频见 https://remote.example/a.mp4\n谨慎追高"},
            "materials": [{"media_type": "video/mp4", "url": "https://remote.example/a.mp4"}],
            "source_url": "https://remote.example/report",
        }
        normalized = text_only_remote_report(report)
        self.assertEqual(normalized["materials"], [])
        self.assertIsNone(normalized["source_url"])
        self.assertNotIn("https://", normalized["raw_markdown"])
        self.assertEqual(normalized["sections"]["market_view"], "视频见\n谨慎追高")

    def test_labels_accept_remote_string_and_object_forms(self):
        self.assertEqual(labels(["白酒", {"name": "半导体"}, {"label": "白酒"}, ""]), ["白酒", "半导体"])

    def test_remote_report_topics_are_deterministic_claim_subjects(self):
        text = "方向上午讲的科技方向都差不多的，超跌就半导体材料，电子特气，先进封装，电子布，MLCC。"
        self.assertIn("半导体材料", extract_topics(text))
        self.assertIn("MLCC", extract_topics(text))
        self.assertEqual(normalize_topic_key("AI应用"), "remote:ai应用")
        report = {"summary": "有色铜可长线配置", "mentioned_sectors": [{"name": "电子"}], "predictions": [{"label": "先进封装"}]}
        self.assertEqual(report_topic_labels(report, text)[:3], ["电子", "先进封装", "有色铜"])

    def test_anqiang_actions_keep_author_time_separate_from_availability(self):
        actions = parse_anqiang_trade_actions(
            date(2026, 8, 12),
            "### 09:46 〈盘中〉\n复材加仓做T，国瓷加仓做T，申菱持股。\n"
            "### 10:40 〈盘中〉\n天孚250做差价，江丰280做差价。\n",
            available_at=datetime(2026, 8, 12, 6, 32, tzinfo=timezone.utc),
        )
        by_symbol = {item["symbol"]: item for item in actions}
        self.assertEqual(by_symbol["301526.SZ"]["action_type"], "add_t")
        self.assertEqual(by_symbol["301018.SZ"]["action_type"], "hold")
        self.assertEqual(by_symbol["300394.SZ"]["target_price"], 250.0)
        self.assertEqual(by_symbol["300394.SZ"]["stated_at"].hour, 10)
        self.assertNotEqual(by_symbol["300394.SZ"]["stated_at"], by_symbol["300394.SZ"]["available_at"])

    def test_anqiang_actions_support_lists_local_prices_and_reentry(self):
        actions = parse_anqiang_trade_actions(
            date(2026, 8, 12),
            "### 10:40 〈盘中〉\n天孚250做差价，仕佳145做差价，江丰280做差价，云锗115，致尚180做差价。\n"
            "### 14:31 〈盘中〉\n致尚上午减仓的资金现在接回吧。\n"
            "### 09:46 〈盘中〉\n复材加仓做T，国瓷加仓做T，长川，云锗，江丰，申菱等持股。\n",
            available_at=datetime(2026, 8, 12, 6, 32, tzinfo=timezone.utc),
        )
        trades = [item for item in actions if item["stated_at"].hour == 10]
        self.assertEqual({item["symbol"] for item in trades}, {"300394.SZ", "688313.SH", "300666.SZ", "002428.SZ", "301486.SZ"})
        self.assertEqual(next(item for item in trades if item["symbol"] == "002428.SZ")["target_price"], 115.0)
        self.assertEqual(next(item for item in actions if item["stated_at"].hour == 14)["action_type"], "buy")
        held = {item["symbol"] for item in actions if item["stated_at"].hour == 9 and item["action_type"] == "hold"}
        self.assertTrue({"300604.SZ", "002428.SZ", "300666.SZ", "301018.SZ"}.issubset(held))

    def test_skill_prompt_variants_are_fixed_research_contracts(self):
        self.assertEqual(len(PROMPT_VARIANTS), 3)
        payload = _variant_payload(PROMPT_VARIANTS[0], [{"report_id": "x"}], [{"symbol": "600000.SH", "action_type": "buy", "stated_at": "x"}])
        self.assertEqual(payload["evaluation_status"], "collecting_point_in_time_outcomes")
        self.assertIn("manual_approval", payload["promotion"])


if __name__ == "__main__":
    unittest.main()
