import unittest

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


if __name__ == "__main__":
    unittest.main()
