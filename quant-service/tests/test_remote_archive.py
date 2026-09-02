import unittest
import httpx
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.analyst_trade_actions import parse_anqiang_trade_actions
from app.analyst_expert_research import EXPERT_DEFAULTS, HORIZONS, _clustered_mean, _cn_date, _herding_effective_sample, _pearson, _softmax_weights
from app.analyst_skill_models import PROMPT_VARIANTS, _variant_payload
from app.analyst_promotion import analyst_live_promotion
from app.remote_archive import (analyst_global_sync_cursor, classify_remote_text, evidence_fragments, explicitness, extract_topics, horizon_days,
                                is_market_opinion, labels, normalize_topic_key, parse_optional_timestamp,
                                report_topic_labels, text_hash, text_only_remote_message, text_only_remote_report,
                                body_stated_timestamp, import_remote_analyst_message)
from app.analyst_observations import observation_action, observation_status
from app.outcome_recomputation import recompute as recompute_outcomes
from app.remote_archive_sync import RemoteArchiveSyncService, _AuthorizedArchiveClient
from app.remote_archive_transport import RemoteArchiveTransport
from app.remote_archive_actions import RemoteArchiveActions
from app.claim_review_service import review_claim


class RemoteArchiveNormalizationTests(unittest.TestCase):
    def test_archive_transport_records_retryable_statuses_without_credentials(self):
        import asyncio

        class Client:
            def __init__(self):
                self.calls = 0

            async def get(self, _path, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return httpx.Response(429, headers={"Retry-After": "1"}, text="rate limited")
                return httpx.Response(200, json={"items": []})

        async def no_sleep(_seconds):
            return None

        transport = RemoteArchiveTransport()
        payload = asyncio.run(transport.get(
            Client(), "/messages/updates", settings={"request_interval_seconds": 0}, sleep=no_sleep,
        ))
        self.assertEqual(payload, {"items": []})
        stats = transport.stats()
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(stats["retries"], 1)
        self.assertEqual(stats["status_counts"]["429"], 1)
        self.assertNotIn("Bearer", str(stats))

    def test_claim_outcome_entry_uses_shanghai_exchange_day(self):
        """A late-UTC message must enter after its Shanghai, not UTC, day."""
        statements = []

        class Result:
            def fetchall(self):
                return []

        class Connection:
            def execute(self, sql, *_args):
                statements.append(sql)
                return Result()

        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = Connection()
        result = recompute_outcomes(
            date(2026, 8, 13), cn_today=lambda: date(2026, 8, 13), db=database,
            recompute_intraday_signal_outcomes=lambda _as_of: {"outcome_rows": 0},
        )
        claim_query = next(sql for sql in statements if "WITH eligible AS" in sql)
        self.assertIn("(c.available_at AT TIME ZONE 'Asia/Shanghai')::date", claim_query)
        self.assertNotIn("c.available_at::date", claim_query)
        self.assertEqual(result["claim_outcomes"], 0)

    def test_action_settings_are_bounded_and_do_not_contain_a_bearer(self):
        with patch.dict("os.environ", {
            "REMOTE_ANALYST_ARCHIVE_BASE_URL": "https://archive.example/",
            "REMOTE_ANALYST_SYNC_MAX_ITEMS": "10000",
            "REMOTE_ANALYST_SYNC_MIN_INTERVAL_SECONDS": "-1",
            "REMOTE_ANALYST_SYNC_REQUEST_INTERVAL_SECONDS": "99",
        }, clear=False):
            settings = RemoteArchiveActions.sync_settings()
        self.assertEqual(settings["base_url"], "https://archive.example")
        self.assertEqual(settings["max_items"], 100)
        self.assertEqual(settings["minimum_interval_seconds"], 1.0)
        self.assertEqual(settings["request_interval_seconds"], 30.0)
        self.assertNotIn("bearer", settings)

    def test_action_terminal_global_cursor_clears_next_cursor_after_import(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {
            "remote_cursor": "prior-signed-cursor",
            "received_after": datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),
        }
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        actions = RemoteArchiveActions(
            database=database,
            run_database_blocking=AsyncMock(),
            message_cursor_update=MagicMock(),
            report_cursor_update=MagicMock(),
        )
        payload = MagicMock(
            stream_key="message_updates", terminal=True, cursor="new-signed-cursor",
            received_after=datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc),
        )
        result = actions.update_global_cursor(payload)
        self.assertFalse(result["has_cursor"])
        self.assertEqual(result["received_after"], "2026-08-12T02:00:00+00:00")
        parameters = connection.execute.call_args_list[-1].args[1]
        self.assertEqual(parameters[1], None)

    def test_archive_authorization_is_request_scoped_not_client_default(self):
        calls = []

        class Client:
            async def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return MagicMock()

        async def invoke():
            pooled = Client()
            first = _AuthorizedArchiveClient(pooled, base_url="https://archive.example/", bearer="first-token")
            second = _AuthorizedArchiveClient(pooled, base_url="https://archive.example/", bearer="rotated-token")
            await first.get("/messages/updates", headers={"Accept": "application/json"})
            await second.get("analysts/a/messages/m")
            return pooled

        pooled = __import__("asyncio").run(invoke())
        self.assertEqual(calls[0][0], "https://archive.example/messages/updates")
        self.assertEqual(calls[0][1]["headers"], {"Accept": "application/json", "Authorization": "Bearer first-token"})
        self.assertEqual(calls[1][1]["headers"], {"Authorization": "Bearer rotated-token"})
        self.assertFalse(hasattr(pooled, "headers"))

    def test_global_message_cursor_defaults_to_an_opaque_empty_cursor(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        cursor = analyst_global_sync_cursor(database, "message_updates")
        self.assertEqual(cursor["stream_key"], "message_updates")
        self.assertIsNone(cursor["remote_cursor"])
        self.assertIsNone(cursor["received_after"])

    def test_message_continuation_sends_opaque_cursor_without_limit(self):
        """v0.3.22 rejects cursor requests that include any other query field."""
        calls = []

        async def run_database(action, *args, timeout_seconds):
            return action(*args)

        service = RemoteArchiveSyncService(
            settings=lambda: {}, transport=MagicMock(), database=MagicMock(),
            run_database_blocking=run_database,
            message_cursor_state=lambda: {"remote_cursor": "signed-next-page"},
            report_cursor_state=MagicMock(), import_message=MagicMock(), import_report=MagicMock(),
            update_global_cursor=MagicMock(), update_report_cursor=MagicMock(),
            message_cursor_update=MagicMock(), report_cursor_update=MagicMock(),
            parse_timestamp=MagicMock(),
        )

        async def fake_get(_client, path, *, params=None):
            calls.append((path, params))
            return {"items": [], "next_cursor": None}

        service._get = fake_get
        __import__("asyncio").run(service._messages(MagicMock(), maximum=20))
        self.assertEqual(calls, [("/messages/updates", {"cursor": "signed-next-page"})])

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

    def test_message_resync_hydrates_missing_source_time_without_rewriting_availability(self):
        """A later remote metadata upgrade may enrich an unchanged message.

        It is deliberately limited to provenance: ``received_at`` remains the
        sole live-strategy timestamp even when a source publication time is
        later discovered.
        """
        message = {
            "message_id": "m" * 64, "analyst_id": "anqiang-touzi-riji", "source_item_id": "post-1",
            "source_message_id": "upstream-1", "received_at": "2026-08-12T09:31:00+08:00",
            "strategy_available_at": "2026-08-12T09:31:00+08:00", "published_at": "2026-08-12T09:30:00+08:00",
            "stated_at": "2026-08-12T09:29:30+08:00", "stated_precision": "second",
            "time_evidence": {"source": "upstream_message", "time_text": "09:29:30"}, "type": "text",
            "content": "云南锗业持股", "version": "0.3.22", "content_hash": "h" * 64,
        }
        connection = MagicMock()

        def execute(sql, *_args):
            result = MagicMock()
            if "SELECT content_hash,received_at" in sql:
                result.fetchone.return_value = {"content_hash": "h" * 64,
                                                  "received_at": datetime(2026, 8, 12, 1, 31, tzinfo=timezone.utc)}
            elif "INSERT INTO quant.analyst_evidence" in sql:
                result.fetchone.return_value = {"evidence_id": "evidence-1"}
            return result

        connection.execute.side_effect = execute
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        with patch("app.remote_archive._materialize_message_claims", return_value=0), \
             patch("app.remote_archive.persist_extraction_run", return_value="run-1"), \
             patch("app.remote_archive.persist_observations_for_evidence", return_value=0), \
             patch("app.remote_archive.sync_anqiang_message_trade_actions", return_value=0), \
             patch("app.remote_archive.rebuild_analyst_skill_profile"):
            result = import_remote_analyst_message(database, message)

        self.assertEqual(result["strategy_available_at"], "2026-08-12T01:31:00+00:00")
        upsert_sql = next(call.args[0] for call in connection.execute.call_args_list
                          if "INSERT INTO quant.remote_analyst_messages" in call.args[0])
        self.assertIn("source_published_at=COALESCE", upsert_sql)
        self.assertIn("stated_at=COALESCE", upsert_sql)
        self.assertNotIn("strategy_available_at=EXCLUDED", upsert_sql)

    def test_dated_body_timestamp_is_author_replay_time_only(self):
        received = datetime(2026, 8, 14, 2, 49, 59, tzinfo=timezone.utc)
        parsed = body_stated_timestamp("8-14 09:50:26\n云南锗业持股", received_at=received)
        self.assertIsNotNone(parsed)
        stated_at, precision, evidence = parsed
        self.assertEqual(stated_at, datetime(2026, 8, 14, 1, 50, 26, tzinfo=timezone.utc))
        self.assertEqual(precision, "second")
        self.assertEqual(evidence["usage"], "author_time_replay_only")
        self.assertIsNone(body_stated_timestamp("09:50:26 云南锗业持股", received_at=received))

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

    def test_anqiang_message_last_explicit_verb_controls_reentry_vs_reduce(self):
        from app.analyst_trade_actions import parse_anqiang_message_trade_actions

        stated = datetime(2026, 8, 13, 10, 46, 52, tzinfo=timezone.utc)
        actions = parse_anqiang_message_trade_actions(
            stated, "致尚把昨天下午接回的资金减仓出去。", available_at=stated,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action_type"], "reduce")
        self.assertEqual(actions[0]["direction"], -1)

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

    def test_report_catalog_scan_is_fair_and_defers_unimported_versions(self):
        """Unchanged early catalogs must not consume another analyst's body budget."""
        catalog_calls, detail_calls, cursor_updates = [], [], []

        async def run_database(action, *args, timeout_seconds):
            return action(*args)

        known = {
            "early": {"report_versions": {"2026-08-10": "v1:early"}},
            "late": {"report_versions": {}},
        }
        service = RemoteArchiveSyncService(
            settings=lambda: {}, transport=MagicMock(), database=MagicMock(),
            run_database_blocking=run_database, message_cursor_state=MagicMock(),
            report_cursor_state=lambda analyst_id: known[analyst_id],
            import_message=MagicMock(), import_report=lambda _database, detail: detail_calls.append(detail),
            update_global_cursor=MagicMock(),
            update_report_cursor=lambda payload: cursor_updates.append(payload),
            message_cursor_update=MagicMock(),
            report_cursor_update=lambda **kwargs: type("Cursor", (), kwargs)(),
            parse_timestamp=MagicMock(),
        )

        async def fake_get(_client, path, *, params=None):
            if path == "/analysts":
                return {"items": [{"analyst_id": "early"}, {"analyst_id": "late"}]}
            if path == "/analysts/early/reports":
                catalog_calls.append((path, params))
                return {"items": [{"date": "2026-08-10", "version": "v1", "content_hash": "early"}]}
            if path == "/analysts/late/reports":
                catalog_calls.append((path, params))
                return {"items": [
                    {"date": "2026-08-11", "version": "v1", "content_hash": "first"},
                    {"date": "2026-08-12", "version": "v1", "content_hash": "deferred"},
                ]}
            if path == "/analysts/late/reports/2026-08-11":
                return {"report_id": "late-first"}
            raise AssertionError(f"unexpected remote path: {path}")

        service._get = fake_get
        result = __import__("asyncio").run(service._reports(MagicMock(), maximum=1))

        self.assertEqual([call[0] for call in catalog_calls], ["/analysts/early/reports", "/analysts/late/reports"])
        self.assertTrue(all(call[1] == {"limit": 100, "offset": 0} for call in catalog_calls))
        self.assertEqual(detail_calls, [{"report_id": "late-first"}])
        self.assertEqual((result["scanned"], result["changed"], result["imported"], result["deferred"]), (3, 2, 1, 1))
        late_update = next(item for item in cursor_updates if item.analyst_id == "late")
        self.assertEqual(late_update.report_versions, {"2026-08-11": "v1:first"})
        self.assertNotIn("2026-08-12", late_update.report_versions)

    def test_sync_service_records_successful_empty_delta_as_liveness(self):
        recorded = []

        async def run_database(action, *args, timeout_seconds):
            return action(*args)

        async def fake_messages(_client, _maximum):
            return {"status": "completed", "items": 0, "imported": 0, "terminal": True,
                    "source": "remote_text_messages"}

        service = RemoteArchiveSyncService(
            settings=lambda: {"base_url": "https://archive.example", "ca_file": None, "max_items": 20,
                              "minimum_interval_seconds": 1}, transport=MagicMock(), database=MagicMock(),
            run_database_blocking=run_database, message_cursor_state=MagicMock(), report_cursor_state=MagicMock(),
            import_message=MagicMock(), import_report=MagicMock(), update_global_cursor=MagicMock(),
            update_report_cursor=MagicMock(), message_cursor_update=MagicMock(), report_cursor_update=MagicMock(),
            parse_timestamp=MagicMock(), record_attempt=lambda *_args: recorded.append(_args),
        )
        service._messages = fake_messages

        class Payload:
            streams = ["messages"]
            max_items = 20

        class Client:
            async def get(self, *_args, **_kwargs):
                raise AssertionError("empty-delta test must not issue an HTTP request")

        @asynccontextmanager
        async def client_context(*_args, **_kwargs):
            yield Client()

        with patch("app.remote_archive_sync.remote_archive_http_client", client_context):
            result = __import__("asyncio").run(service.sync(Payload(), "Bearer " + "a" * 32))
        self.assertEqual(result["streams"]["messages"]["items"], 0)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][1:3], ("messages", "completed"))

    def test_sync_service_does_not_record_an_attempt_row_for_a_remote_auth_rejection(self):
        # A remote 401/403 means the archive rejected this bearer (a
        # stale/misconfigured n8n credential), not a data or transport
        # problem.  It must not pollute the sync health dashboard with a
        # "recent failure" attempt row, though the automation run it started
        # is still closed out (see remote_archive_sync.py sync()).
        recorded = []
        failed_runs = []

        async def run_database(action, *args, timeout_seconds):
            return action(*args)

        async def fake_messages(_client, _maximum):
            raise HTTPException(status_code=401, detail="invalid bearer")

        service = RemoteArchiveSyncService(
            settings=lambda: {"base_url": "https://archive.example", "ca_file": None, "max_items": 20,
                              "minimum_interval_seconds": 1}, transport=MagicMock(), database=MagicMock(),
            run_database_blocking=run_database, message_cursor_state=MagicMock(), report_cursor_state=MagicMock(),
            import_message=MagicMock(), import_report=MagicMock(), update_global_cursor=MagicMock(),
            update_report_cursor=MagicMock(), message_cursor_update=MagicMock(), report_cursor_update=MagicMock(),
            parse_timestamp=MagicMock(), record_attempt=lambda *_args: recorded.append(_args),
        )
        service._messages = fake_messages
        service._fail_automation_run = lambda run_id, error: failed_runs.append((run_id, error))

        class Payload:
            streams = ["messages"]
            max_items = 20

        class Client:
            async def get(self, *_args, **_kwargs):
                raise AssertionError("auth-rejection test must not issue an HTTP request")

        @asynccontextmanager
        async def client_context(*_args, **_kwargs):
            yield Client()

        with patch("app.remote_archive_sync.remote_archive_http_client", client_context):
            with self.assertRaises(HTTPException):
                __import__("asyncio").run(service.sync(Payload(), "Bearer " + "a" * 32))
        self.assertEqual(recorded, [])
        self.assertEqual(len(failed_runs), 1)

    def test_duplicate_same_stream_sync_waits_instead_of_returning_local_rate_limit(self):
        import asyncio

        recorded = []

        async def run_database(action, *args, timeout_seconds):
            return action(*args)

        async def fake_messages(_client, _maximum):
            await asyncio.sleep(0.002)
            return {"status": "completed", "items": 0, "imported": 0, "terminal": True}

        service = RemoteArchiveSyncService(
            settings=lambda: {"base_url": "https://archive.example", "ca_file": None, "max_items": 20,
                              "minimum_interval_seconds": 0.01}, transport=MagicMock(), database=MagicMock(),
            run_database_blocking=run_database, message_cursor_state=MagicMock(), report_cursor_state=MagicMock(),
            import_message=MagicMock(), import_report=MagicMock(), update_global_cursor=MagicMock(),
            update_report_cursor=MagicMock(), message_cursor_update=MagicMock(), report_cursor_update=MagicMock(),
            parse_timestamp=MagicMock(), record_attempt=lambda *_args: recorded.append(_args),
        )
        service._messages = fake_messages

        class Payload:
            streams = ["messages"]
            max_items = 20

        class Client:
            async def get(self, *_args, **_kwargs):
                raise AssertionError("duplicate-pacing test must not issue a remote request")

        @asynccontextmanager
        async def client_context(*_args, **_kwargs):
            yield Client()

        async def run_duplicates():
            return await asyncio.gather(
                service.sync(Payload(), "Bearer " + "a" * 32),
                service.sync(Payload(), "Bearer " + "a" * 32),
            )

        with patch("app.remote_archive_sync.remote_archive_http_client", client_context):
            first, second = asyncio.run(run_duplicates())
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual([record[2] for record in recorded], ["completed", "completed"])

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
