import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from app.analyst_trade_actions import parse_anqiang_trade_actions
from app.analyst_expert_research import EXPERT_DEFAULTS, HORIZONS, _clustered_mean, _cn_date, _herding_effective_sample, _pearson, _softmax_weights
from app.analyst_skill_models import PROMPT_VARIANTS, _variant_payload
from app.analyst_promotion import analyst_live_promotion
from app.remote_archive import (analyst_global_sync_cursor, classify_remote_text, evidence_fragments, explicitness, extract_topics, horizon_days,
                                is_market_opinion, labels, normalize_topic_key, parse_optional_timestamp,
                                report_topic_labels, text_hash, text_only_remote_message, text_only_remote_report)
from app.analyst_observations import observation_action, observation_status
from app.remote_archive_sync import RemoteArchiveSyncService
from app.claim_review_service import review_claim


class RemoteArchiveNormalizationTests(unittest.TestCase):
    def test_global_message_cursor_defaults_to_an_opaque_empty_cursor(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        cursor = analyst_global_sync_cursor(database, "message_updates")
        self.assertEqual(cursor["stream_key"], "message_updates")
        self.assertIsNone(cursor["remote_cursor"])
        self.assertIsNone(cursor["received_after"])

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

    def test_message_ingress_preserves_received_time_but_never_keeps_media_links(self):
        message = {
            "message_id": "a" * 64, "analyst_id": "anqiang-touzi-riji", "source_item_id": "post-1",
            "received_at": "2026-08-12T09:31:00+08:00", "strategy_available_at": "2026-08-12T09:31:00+08:00",
            "published_at": "2026-08-12T09:30:00+08:00", "edited_at": None, "stated_at": "2026-08-12T09:29:00+08:00",
            "stated_precision": "minute", "time_evidence": {"time_text": "09:29"}, "type": "image_ocr",
            "content": "![图](https://remote.example/image.jpg)\n看好 000001.SZ", "source_ref": "releases/x",
            "version": "0.3.22", "content_hash": "b" * 64,
        }
        normalized = text_only_remote_message(message)
        self.assertNotIn("https://", normalized["content"])
        self.assertEqual(parse_optional_timestamp(normalized["received_at"]), parse_optional_timestamp(normalized["strategy_available_at"]))
        self.assertEqual(normalized["stated_at"], "2026-08-12T09:29:00+08:00")

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

    def test_market_scope_requires_direction_and_explicitness_is_bounded(self):
        self.assertTrue(is_market_opinion("大盘看多，建议加仓"))
        self.assertFalse(is_market_opinion("大盘今天成交活跃"))
        self.assertGreater(explicitness("大盘若突破则看多", scope="market"), 0.5)
        self.assertEqual(explicitness("普通评论", scope="market"), 0.0)

    def test_expert_research_uses_fixed_defaults_and_required_horizons(self):
        self.assertEqual(HORIZONS, (1, 2, 3, 5, 10, 20, 40, 60))
        self.assertEqual(EXPERT_DEFAULTS, {"gamma": 0.99, "eta": 0.4, "alpha": 0.01, "kappa": 100})

    def test_expert_weights_use_fixed_share_and_independence_as_explicit_prior(self):
        weights = _softmax_weights({"independent": 0.0, "promotional": 0.0}, {
            "independent": {"independence_class": "independent"},
            "promotional": {"independence_class": "promotional"},
        })
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertGreater(weights["independent"], weights["promotional"])

    def test_clustered_statistics_only_count_date_clusters(self):
        self.assertEqual(_clustered_mean([])["clusters"], 0)
        self.assertAlmostEqual(_pearson([(1.0, 1.0), (2.0, 2.0)]) or 0.0, 1.0)

    def test_herding_diagnostic_reduces_effective_expert_count_for_same_sign_overlap(self):
        rows = [
            {"opinion_date": date(2026, 8, 10), "scope": "theme", "subject_key": "x", "remote_analyst_id": "a", "direction": 1},
            {"opinion_date": date(2026, 8, 10), "scope": "theme", "subject_key": "x", "remote_analyst_id": "b", "direction": 1},
        ]
        report = _herding_effective_sample(rows)
        self.assertEqual(report["effective_independent_analysts"], 1.0)

    def test_cn_day_is_not_the_utc_calendar_day(self):
        value = datetime(2026, 8, 12, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(_cn_date(value), date(2026, 8, 13))

    def test_only_explicit_approved_registry_can_enable_analyst_weight(self):
        class Connection:
            def __init__(self, row): self.row = row
            def execute(self, *_args, **_kwargs):
                class Result:
                    def __init__(self, row): self.row = row
                    def fetchone(self): return self.row
                return Result(self.row)

        blocked = analyst_live_promotion(Connection({"status": "eligible_for_review", "max_live_weight": 0.1,
                                                      "approved_by": None, "approved_at": None, "reason": "not approved",
                                                      "methodology_version": "x", "evidence": {}}), date(2026, 8, 12))
        self.assertFalse(blocked["execution_eligible"])
        approved = analyst_live_promotion(Connection({"status": "approved", "max_live_weight": 0.5,
                                                       "approved_by": "reviewer", "approved_at": datetime.now(timezone.utc),
                                                       "reason": "approved", "methodology_version": "x", "evidence": {}}), date(2026, 8, 12))
        self.assertTrue(approved["execution_eligible"])
        self.assertEqual(approved["weight"], 0.1)

    def test_unified_observation_keeps_delayed_author_time_replay_only(self):
        received = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
        stated = received - __import__("datetime").timedelta(minutes=6)
        self.assertEqual(observation_action(1, {"direction_source": "explicit_action_positive"}), "watch")
        self.assertEqual(observation_status(scope="stock", subject_key="000001.SZ", direction=1,
                                            confidence=0.9, source_kind="message", stated_at=stated,
                                            available_at=received), "replay_only")

    def test_stream_rate_limits_are_independent_for_split_schedulers(self):
        service = RemoteArchiveSyncService(
            settings=lambda: {}, transport=MagicMock(), database=MagicMock(),
            run_database_blocking=MagicMock(), message_cursor_state=MagicMock(), report_cursor_state=MagicMock(),
            import_message=MagicMock(), import_report=MagicMock(), update_global_cursor=MagicMock(),
            update_report_cursor=MagicMock(), message_cursor_update=MagicMock(), report_cursor_update=MagicMock(),
            parse_timestamp=MagicMock(),
        )
        service._last_started = {"reports": 100.0}
        service._last_started["messages"] = 0.0
        self.assertIn("reports", service._last_started)
        self.assertNotIn("message", service._last_started)

    def test_message_evidence_review_preserves_immutable_availability(self):
        available_at = datetime(2026, 8, 12, 2, 1, tzinfo=timezone.utc)
        item = {
            "status": "pending", "evidence_id": "evidence", "remote_analyst_id": "anqiang",
            "available_at": available_at, "suggested_symbol": "000001.SZ", "suggested_label": "平安银行",
            "direction": 1, "strength": 0.7, "horizon_days": 1, "extraction_confidence": 0.8,
        }

        class Result:
            def fetchone(self): return item

        connection = MagicMock()
        connection.execute.side_effect = lambda sql, *_args: Result() if "FOR UPDATE" in sql else MagicMock()
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        payload = type("Payload", (), {"status": "approved", "symbol": None, "reviewer_note": "verified"})()
        result = review_claim("review", payload, database=database, exchange_for=lambda _symbol: "SZ")
        self.assertEqual(result["status"], "approved")
        insert_call = next(call for call in connection.execute.call_args_list if "manual-claim-review-v1" in call.args[0])
        self.assertEqual(insert_call.args[1][-2], available_at)
        self.assertIn("remote_analyst_messages", connection.execute.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
