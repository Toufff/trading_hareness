"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403


class IntradayRuntimeScheduleTests(unittest.TestCase):
    def test_intraday_watchlist_rules_require_confirmation_except_hard_stop(self):
        scan = IntradayScanRequest(symbols=["600176.sh", "600176.SH"], realtime_validation_limit=2)
        self.assertEqual(scan.symbols, ["600176.SH"])
        expanded_scan = IntradayScanRequest(symbols=[f"{600000 + index:06d}.SH" for index in range(21)])
        self.assertEqual(len(expanded_scan.symbols), 21)
        quote = intraday_quote_from_fuyao({"symbol": "600176.SH", "name": "中国巨石", "price": "42.10", "pct_change": "2.1"})
        quote.update({"price_source": "tencent_batched_watch_quote", "volume_ratio": 2.3, "turnover_rate": 4.2,
                      "main_net_inflow": 123.0})
        self.assertEqual(quote["symbol"], "600176.SH")
        entry_watch = {"symbol": "600176.SH", "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True}
        entry = intraday_signal_rules(entry_watch, quote, {"price": 42.00})
        self.assertEqual(entry[0]["signal_type"], "entry")
        self.assertFalse(entry[0]["hard"])
        position_watch = {"symbol": "600176.SH", "entry_price": 42.50, "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True, "hard_stop": 42.20}
        exit_signal = intraday_signal_rules(position_watch, quote, {"price": 42.00})
        self.assertEqual(exit_signal[0]["signal_type"], "exit")
        self.assertTrue(exit_signal[0]["hard"])

    def test_opening_gap_creates_watch_not_entry_when_minutes_are_not_ready(self):
        watch = {"symbol": "600176.SH", "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True}
        quote = {
            "symbol": "600176.SH", "price": 43.20, "pct_change": 4.2,
            "price_source": "tencent_batched_watch_quote", "price_freshness": {"status": "fresh"},
            "_scan_observed_at": datetime(2026, 8, 17, 1, 32, tzinfo=timezone.utc),
        }
        signals = intraday_signal_rules(watch, quote, None)
        self.assertEqual(signals[0]["signal_key"], "600176.SH:watch:opening_gap_continuation_v1")
        self.assertEqual(signals[0]["signal_type"], "watch")
        self.assertIn("watch_only_not_entry", signals[0]["risk_flags"])

    def test_intraday_minute_context_includes_unconfigured_explicit_watches(self):
        watches = [{"symbol": "000001.SZ", "metadata": {}}, {"symbol": "000002.SZ", "metadata": {}}]
        rows = [
            {"time": f"09:{30 + index:02d}", "close": 10 + index / 100,
             "vol": 100 + index, "amount": (100 + index) * (10 + index / 100) * 100}
            for index in range(6)
        ]

        async def check() -> tuple[dict[str, object], dict[str, object], AsyncMock]:
            minute_fetch = AsyncMock(return_value=rows)
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())), \
                 patch("app.main.tencent_intraday_minutes", new=minute_fetch), \
                 patch("app.main._intraday_tencent_minute_cache", new={}):
                features, source = await intraday_tencent_surge_context(watches)
            return features, source, minute_fetch

        features, source, minute_fetch = asyncio.run(check())
        self.assertEqual(source["requested"], ["000001.SZ", "000002.SZ"])
        self.assertEqual(sorted(features), ["000001.SZ", "000002.SZ"])
        self.assertEqual(minute_fetch.await_count, 2)

    def test_intraday_minute_context_prioritizes_configured_targets_and_peers(self):
        watches = [
            {"symbol": "000001.SZ", "metadata": {}},
            {"symbol": "000002.SZ", "metadata": {}},
            {"symbol": "000003.SZ", "metadata": {"surge_strategy": {
                "enabled": True, "peer_symbols": ["000004.SZ", "000005.SZ"],
            }}},
        ]
        rows = [
            {"time": f"09:{30 + index:02d}", "close": 10 + index / 100,
             "vol": 100 + index, "amount": (100 + index) * (10 + index / 100) * 100}
            for index in range(6)
        ]

        async def check() -> tuple[dict[str, object], AsyncMock]:
            minute_fetch = AsyncMock(return_value=rows)
            with patch("app.main.open_provider_capabilities", new=AsyncMock(return_value=set())), \
                 patch("app.main.tencent_intraday_minutes", new=minute_fetch), \
                 patch("app.main.intraday_minute_profile_max_symbols", return_value=3), \
                 patch("app.main._intraday_tencent_minute_cache", new={}):
                _, source = await intraday_tencent_surge_context(watches)
            return source, minute_fetch

        source, minute_fetch = asyncio.run(check())
        self.assertEqual(source["requested"], ["000003.SZ", "000004.SZ", "000005.SZ"])
        self.assertTrue(source["truncated"])
        self.assertEqual(minute_fetch.await_count, 3)

    def test_tencent_minute_amount_scale_corrects_only_audited_hundredfold_variant(self):
        self.assertEqual(tencent_minute_amount_scale(price=293.0, cumulative_volume_lot=1000, cumulative_amount=293_000.0), 100.0)
        self.assertEqual(tencent_minute_amount_scale(price=29.3, cumulative_volume_lot=1000, cumulative_amount=2_930_000.0), 1.0)

    def test_cross_sectional_flow_extremes_are_unit_independent(self):
        quotes = {
            "000001.SZ": {"main_net_inflow": -900, "volume_ratio": 2.0},
            "000002.SZ": {"main_net_inflow": 0, "volume_ratio": 1.0},
            "000003.SZ": {"main_net_inflow": 900, "volume_ratio": 2.0},
        }
        annotate_intraday_flow_percentiles(quotes)
        self.assertEqual(quotes["000001.SZ"]["main_flow_percentile"], 0.0)
        self.assertEqual(quotes["000003.SZ"]["main_flow_percentile"], 1.0)

    def test_batched_watch_quote_refreshes_price_without_inventing_flow(self):
        quotes = {"000001.SZ": {"symbol": "000001.SZ", "price": 10.0, "pct_change": 0.0,
                                 "main_net_inflow": 123.0, "main_flow_percentile": 0.9, "raw": {}}}
        merged = merge_intraday_watch_quote_prices(
            quotes, [{"ts_code": "000001.SZ", "name": "平安银行", "price": 10.2, "pre_close": 10.0}],
        )
        self.assertEqual(merged["000001.SZ"]["price"], 10.2)
        self.assertEqual(merged["000001.SZ"]["main_net_inflow"], 123.0)
        self.assertEqual(merged["000001.SZ"]["price_source"], "tencent_batched_watch_quote")
        self.assertEqual(intraday_quote_observation_source(merged["000001.SZ"]), "tencent_free")

    def test_eastmoney_watch_flow_fallback_keeps_price_and_omits_small_basket_percentile(self):
        quotes = {"000001.SZ": {"symbol": "000001.SZ", "price": 10.2,
                                  "price_source": "tencent_batched_watch_quote", "raw": {}}}
        merged = merge_intraday_eastmoney_watch_flows(
            quotes, [{"ts_code": "000001.SZ", "volume_ratio": 2.4, "turnover_rate": 5.1,
                      "main_net_inflow": 123_000, "main_net_inflow_ratio": 1.8, "raw": {"f62": 123_000}}],
        )
        self.assertEqual(merged["000001.SZ"]["price"], 10.2)
        self.assertEqual(merged["000001.SZ"]["price_source"], "tencent_batched_watch_quote")
        self.assertEqual(merged["000001.SZ"]["volume_ratio"], 2.4)
        self.assertEqual(merged["000001.SZ"]["main_net_inflow"], 123_000)
        self.assertIsNone(merged["000001.SZ"]["main_flow_percentile"])
        annotate_flow_snapshot_provenance(merged, {
            "status": "fresh", "age_seconds": 0, "source": "eastmoney_watch_flow_batch",
            "scope": "explicit_watchlist_only", "cross_sectional": False,
            "semantics": "watchlist_public_flow_proxy_not_exchange_order_flow",
        })
        self.assertTrue(merged["000001.SZ"]["flow_snapshot"]["decision_eligible"])
        self.assertFalse(merged["000001.SZ"]["flow_snapshot"]["cross_sectional"])

    def test_quote_exchange_timestamp_requires_one_current_shanghai_frame(self):
        observed_at = datetime(2026, 8, 12, 5, 0, 10, tzinfo=timezone.utc)
        self.assertEqual(intraday_quote_exchange_time_status(
            {"price_trade_time": "20260812130000"}, observed_at, 20,
        )["status"], "fresh")
        self.assertEqual(intraday_quote_exchange_time_status(
            {"price_trade_date": "2026-08-12", "price_trade_time": "12:59:00"}, observed_at, 20,
        )["status"], "stale_timestamp")
        self.assertEqual(intraday_quote_exchange_time_status({}, observed_at, 20)["status"], "missing_timestamp")

    def test_sina_watch_fallback_keeps_flow_fields_absent(self):
        merged = merge_intraday_sina_watch_quotes({}, [{"ts_code": "000001.SZ", "name": "平安银行", "close": 10.2, "pre_close": 10.0}])
        self.assertEqual(merged["000001.SZ"]["price"], 10.2)
        self.assertNotIn("main_net_inflow", merged["000001.SZ"])
        self.assertEqual(merged["000001.SZ"]["price_source"], "sina_batched_watch_quote")
        self.assertEqual(intraday_quote_observation_source(merged["000001.SZ"]), "sina_free")
        watch = {"symbol": "000001.SZ", "entry_price": 10, "available_quantity": 0, "alert_on_entry": False, "alert_on_exit": True}
        quote = {"price": 9.8, "pct_change": -2, "volume_ratio": 2, "turnover_rate": 5, "main_net_inflow": -900, "main_flow_percentile": 0.0}
        self.assertEqual(intraday_signal_rules(watch, quote, {"price": 10})[0]["signal_key"], "000001.SZ:reduce:extreme_flow_sell")
        extension_watch = {"symbol": "002842.SZ", "entry_price": None, "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True}
        extension_quote = {"price": 40.89, "pct_change": 6.54, "volume_ratio": 1.45, "turnover_rate": 20.21, "main_net_inflow": 6850, "main_flow_percentile": 0.97108}
        self.assertEqual(intraday_signal_rules(extension_watch, extension_quote, {"price": 40.80})[0]["signal_key"], "002842.SZ:watch:price_extension")

    def test_live_policy_keeps_sina_fallback_as_evidence_not_a_confirmed_alert(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "entry"}, {"available_quantity": 0},
            {"price": 10, "price_source": "sina_batched_watch_quote"},
            {"status": "completed", "trade_constraints": {}},
            {"status": "available", "market_state": "mixed_or_neutral", "board_snapshot_age_seconds": 30},
            {"status": "confirmed"},
        )
        self.assertFalse(result["allow_confirmation"])
        self.assertIn("quote_source_not_decision_eligible", result["reason_codes"])

    def test_live_policy_allows_current_tencent_watch_batch_after_other_gates_pass(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "entry"}, {"available_quantity": 0},
            {"price": 10, "price_source": "tencent_batched_watch_quote", "price_freshness": {"status": "fresh"}},
            {"status": "completed", "trade_constraints": {}},
            {"status": "available", "market_state": "mixed_or_neutral", "board_snapshot_age_seconds": 30},
            {"status": "confirmed"},
        )
        self.assertTrue(result["allow_confirmation"])

    def test_live_policy_blocks_new_entry_when_public_flow_snapshot_is_stale(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "entry"}, {"available_quantity": 0},
            {"price": 10, "price_source": "tencent_batched_watch_quote", "price_freshness": {"status": "fresh"},
             "main_net_inflow": 1, "flow_snapshot": {"status": "cached", "age_seconds": 46,
                                                        "decision_eligible": False}},
            {"status": "completed", "trade_constraints": {}},
            {"status": "available", "market_state": "mixed_or_neutral", "board_snapshot_age_seconds": 30},
            {"status": "confirmed"},
        )
        self.assertFalse(result["allow_confirmation"])
        self.assertIn("public_flow_snapshot_not_fresh", result["reason_codes"])
