"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403


class StrategyRuleRegressionTests(unittest.TestCase):
    def test_live_policy_gate_blocks_entry_on_paper_portfolio_limit(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "entry"}, {"available_quantity": 0}, {"price": 10, "price_source": "tencent_batched_watch_quote"},
            {"status": "completed", "trade_constraints": {}},
            {"status": "available", "market_state": "mixed_or_neutral", "board_snapshot_age_seconds": 30},
            {"status": "confirmed"},
            {"allowed": False, "reasons": ["gross_exposure_limit"], "risk_flags": ["paper_gross_exposure_block"]},
        )
        self.assertFalse(result["allow_confirmation"])
        self.assertIn("gross_exposure_limit", result["reason_codes"])
        self.assertIn("policy_portfolio_risk", result["risk_flags"])

    def test_super_get_fast_loop_does_not_extend_circuit_on_local_skip(self):
        async def check() -> tuple[dict[str, object], AsyncMock]:
            blocking = AsyncMock()
            with patch("app.main.call_tushare_api", new=AsyncMock(
                side_effect=HTTPException(status_code=503, detail="provider circuit-open until later"),
            )), patch("app.main.run_database_blocking", new=blocking):
                result = await capture_intraday_super_get_fast_quote("000001.SZ")
            return result, blocking

        result, blocking = asyncio.run(check())
        self.assertEqual(result["status"], "circuit_open")
        blocking.assert_not_awaited()

    def test_intraday_outcome_metrics_keep_direction_and_adverse_path(self):
        prices = [Decimal("10.10"), Decimal("9.80"), Decimal("10.40")]
        long_metrics = intraday_signal_outcome_metrics(Decimal("10.00"), 1, prices)
        short_metrics = intraday_signal_outcome_metrics(Decimal("10.00"), -1, prices)
        self.assertEqual(intraday_signal_direction("entry"), 1)
        self.assertEqual(intraday_signal_direction("watch"), 1)
        self.assertEqual(intraday_signal_direction("reduce"), -1)
        self.assertEqual(intraday_signal_direction("exit"), -1)
        self.assertIsNone(intraday_signal_direction("data_issue"))
        self.assertEqual(long_metrics, {
            "raw_return": Decimal("0.04"),
            "maximum_favorable_excursion": Decimal("0.04"),
            "maximum_adverse_excursion": Decimal("-0.02"),
        })
        self.assertEqual(short_metrics, {
            "raw_return": Decimal("-0.04"),
            "maximum_favorable_excursion": Decimal("0.02"),
            "maximum_adverse_excursion": Decimal("-0.04"),
        })

    def test_sector_surge_replay_catches_xianglu_before_late_extension(self):
        rows = []
        prices = [38.60] * 20 + [38.80, 39.20, 39.43, 39.85]
        volumes = [1100] * 20 + [1494, 6325, 2482, 9286]
        cumulative_volume = 0
        cumulative_amount = 0.0
        for index, (price, volume) in enumerate(zip(prices, volumes, strict=True)):
            cumulative_volume += volume
            cumulative_amount += price * volume * 100
            rows.append({"time": f"{1341 + index:04d}", "close": price, "volume_lot": volume,
                         "amount": price * volume * 100, "cumulative_volume_lot": cumulative_volume,
                         "cumulative_amount": cumulative_amount, "vwap": cumulative_amount / (cumulative_volume * 100),
                         "is_complete": True})
        feature = intraday_minute_features(rows)
        self.assertGreater(feature["minute_volume_multiple"], 8)
        peers = {
            "002378.SZ": {"return_1m_pct": 0.782, "return_3m_pct": 1.425, "minute_volume_multiple": 7.531},
            "000657.SZ": {"return_1m_pct": 0.861, "return_3m_pct": 1.320, "minute_volume_multiple": 2.365},
            "600549.SH": {"return_1m_pct": 0.702, "return_3m_pct": 1.235, "minute_volume_multiple": 1.955},
        }
        context = intraday_peer_context(list(peers), peers)
        watch = {"symbol": "002842.SZ", "entry_price": None, "available_quantity": 0, "alert_on_entry": True,
                 "alert_on_exit": False, "metadata": {"surge_strategy": {"enabled": True, "sector_label": "同花顺钨"}}}
        leader_feature = {"return_1m_pct": 1.031, "return_3m_pct": 0.979, "minute_volume_multiple": 5.597,
                          "above_vwap_pct": 1.94}
        empty_context = intraday_peer_context(list(peers), {symbol: {**item, "return_1m_pct": 0}
                                                             for symbol, item in peers.items()})
        leader_quote = {"price": 39.20, "pct_change": 2.14, "volume_ratio": 1.2, "turnover_rate": 15,
                        "main_net_inflow": 10, "main_flow_percentile": 0.7}
        leader = intraday_signal_rules(watch, leader_quote, {"price": 38.80}, None, leader_feature, empty_context)[0]
        self.assertEqual(leader["signal_key"], "002842.SZ:watch:leader_burst_v1")
        quote = {"price": 39.85, "pct_change": 3.83, "volume_ratio": 1.4, "turnover_rate": 18,
                 "main_net_inflow": 100, "main_flow_percentile": 0.9}
        signal = intraday_signal_rules(watch, quote, {"price": 39.43}, None, feature, context)[0]
        self.assertEqual(signal["signal_key"], "002842.SZ:entry:sector_surge_v1")
        self.assertTrue(signal["independent_confirmation"])
        quote["pct_change"] = 6.6
        self.assertFalse(any(item["signal_key"] == "002842.SZ:entry:sector_surge_v1"
                             for item in intraday_signal_rules(watch, quote, {"price": 39.85}, None, feature, context)))

    def test_provider_decoder_accepts_object_rows(self):
        rows = _decode_rows({"code": 0, "data": {"fields": ["ts_code", "trade_date"], "items": [
            {"ts_code": "600000.SH", "trade_date": "20220429"},
        ]}})
        self.assertEqual(rows, [{"ts_code": "600000.SH", "trade_date": "20220429"}])
        realtime_rows = _decode_rows({"code": 0, "data": [{"ts_code": "000636.SZ", "time": "2026-08-10 11:26:00"}]})
        self.assertEqual(realtime_rows[0]["time"], "2026-08-10 11:26:00")

    def test_intraday_strategy_scoring_uses_relative_ranks_and_marks_extension(self):
        self.assertEqual(strategy_rank([None, None]), {})
        self.assertEqual(strategy_rank([-1.0])[0], 0.0)
        items = [
            {"taxonomy_key": "eastmoney_concept", "sector_key": "A", "label": "资金流入", "net_inflow": 50,
             "change_pct": 2.0, "mapped_members": 2, "top_stocks": [{"symbol": "000001.SZ", "name": "甲", "main_net_inflow": 30,
             "volume_ratio": 3.5, "turnover_rate": 8, "pct_change": 4, "turnover": 100}]},
            {"taxonomy_key": "eastmoney_concept", "sector_key": "B", "label": "资金流出", "net_inflow": -10,
             "change_pct": -1.0, "mapped_members": 2, "top_stocks": [{"symbol": "000002.SZ", "name": "乙", "main_net_inflow": -5,
             "volume_ratio": 5, "turnover_rate": 30, "pct_change": 9, "turnover": 80}]},
        ]
        regime, metrics = strategy_market_regime(items)
        self.assertEqual(regime, "neutral")
        self.assertEqual(metrics["known_board_flows"], 2)
        candidates = strategy_intraday_candidates(items, 10)
        self.assertEqual(candidates[0]["symbol"], "000001.SZ")
        self.assertEqual(candidates[0]["decision"], "research_candidate")
        declining = next(item for item in candidates if item["symbol"] == "000002.SZ")
        self.assertEqual(declining["decision"], "no_trade")
        self.assertIn("price_extension", declining["risk_flags"])

    def test_market_state_distinguishes_defensive_rotation_from_broad_direction(self):
        items = [
            {"label": "贵金属", "net_inflow": 10, "change_pct": 2.0},
            {"label": "银行", "net_inflow": 8, "change_pct": 0.5},
            {"label": "半导体", "net_inflow": -12, "change_pct": 0.2},
            {"label": "通信设备", "net_inflow": -9, "change_pct": -0.5},
        ]
        state, metrics = strategy_market_state(items)
        self.assertEqual(state, "rotation_defensive")
        self.assertEqual(metrics["defensive_inflow_boards"], ["贵金属", "银行"])
        self.assertEqual(metrics["technology_outflow_boards"], ["半导体", "通信设备"])

    def test_tencent_snapshot_quotes_keep_inferred_exchange_date(self):
        quotes = tencent_snapshot_quotes([
            {"code": "sh600176", "name": "中国巨石", "zxj": "43.28", "zdf": "-2.48", "lb": "1.2", "hsl": "3.0", "zljlr": "12", "turnover": "100"},
        ], date(2026, 8, 10))
        self.assertEqual(quotes[0]["ts_code"], "600176.SH")
        self.assertEqual(quotes[0]["trade_date"], "20260810")
        self.assertEqual(quotes[0]["volume_ratio"], 1.2)
        self.assertEqual(quotes[0]["turnover_rate"], 3.0)
        self.assertEqual(quotes[0]["main_net_inflow"], 12.0)
        self.assertTrue(quotes[0]["source_session_date_inferred"])

    def test_free_provider_symbol_routing_is_explicit(self):
        self.assertEqual(eastmoney_secid("603580.SH"), "1.603580")
        self.assertEqual(eastmoney_secid("000636.SZ"), "0.000636")
        self.assertEqual(tencent_symbol("603580.SH"), "sh603580")
        self.assertEqual(tencent_symbol("000636.SZ"), "sz000636")
        self.assertTrue({"eastmoney_free", "tencent_free", "sina_free", "cninfo_free", "akshare", "xinhua_finance"}.issubset(
            {item["provider_key"] for item in free_provider_status()}
        ))
        self.assertEqual(cninfo_stock_param("000636.SZ")["stock"], "000636,gssz0000636")
        self.assertEqual(cninfo_stock_param("600519.SH")["stock"], "600519,gssh0600519")
        self.assertEqual(classify_announcement_title("关于回购公司股份的公告"), "shareholder_event")
        self.assertEqual(classify_announcement_title("重大事项公告"), "corporate_action")

    def test_sina_batch_parser_and_market_snapshot_quality_are_explicit(self):
        rows = parse_sina_quote_batch(
            'var hq_str_sz000636="风华高科,20,19.5,20.2,20.5,19.7,0,0,123,456,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-08-10,11:30:00";',
            {"sz000636": "000636.SZ"},
        )
        self.assertEqual(rows[0]["ts_code"], "000636.SZ")
        summary = summarize_quotes(rows)
        self.assertEqual(summary["advancers"], 1)
        self.assertAlmostEqual(summary["median_change_pct"], 3.5897, places=4)
        status, eligible, flags = snapshot_status(universe_count=1200, quote_count=1180, minimum_universe=1000,
                                                   minimum_coverage=0.95, licensed_providers=set(), observed_providers={"sina_free"})
        self.assertEqual(status, "degraded")
        self.assertFalse(eligible)
        self.assertIn("no_licensed_realtime_market_feed", flags)

    def test_public_market_batch_is_opt_in_and_bounded(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(market_snapshot_public_quote_settings(), {"enabled": False, "batch_size": 80, "concurrency": 2})
        with patch.dict("os.environ", {"MARKET_SNAPSHOT_ENABLE_PUBLIC_BATCH": "true", "MARKET_SNAPSHOT_PUBLIC_BATCH_SIZE": "999", "MARKET_SNAPSHOT_PUBLIC_CONCURRENCY": "0"}, clear=True):
            self.assertEqual(market_snapshot_public_quote_settings(), {"enabled": True, "batch_size": 200, "concurrency": 1})

    def test_sector_catalog_sync_is_explicitly_bounded(self):
        self.assertEqual(ths_taxonomy_key("N"), "ths_index_n")
        self.assertEqual(SectorCatalogSyncRequest(index_type="I").member_limit, 0)
        self.assertEqual(ConceptMemberSyncRequest().member_limit, 25)
        self.assertEqual(ConceptMemberSyncRequest(member_offset=50).member_offset, 50)
        self.assertEqual(EastmoneyBoardMemberSyncRequest(kind="industry").member_limit, 25)
        self.assertEqual(IntradaySectorReportRequest().hydrate_top_boards, 0)
        self.assertEqual(IntradaySectorReportRequest(hydrate_top_boards=3).hydrate_top_boards, 3)
        self.assertEqual(eastmoney_member_symbol({"代码": "600519"}), "600519.SH")
        self.assertEqual(eastmoney_member_symbol({"代码": "300750"}), "300750.SZ")
        self.assertEqual(eastmoney_member_symbol({"代码": "920870"}), "920870.BJ")
        self.assertIsNone(eastmoney_member_symbol({"代码": "invalid"}))
        with self.assertRaises(ValidationError):
            SectorCatalogSyncRequest(sync_members=True)
        with self.assertRaises(ValidationError):
            SectorCatalogSyncRequest(member_limit=1)
        with self.assertRaises(ValidationError):
            SectorCatalogSyncRequest(all_types=True, sync_members=True, member_limit=1)

    def test_offline_minute_row_uses_shanghai_time_and_validates_ohlc(self):
        parsed = offline_minute_row({
            "ts_code": "600519.SH", "datetime": "2026-08-04 09:31:00",
            "open": "1400", "high": "1402", "low": "1399", "close": "1401", "vol": "10",
        })
        self.assertEqual(parsed["symbol"], "600519.SH")
        self.assertEqual(parsed["bar_time"].isoformat(), "2026-08-04T01:31:00+00:00")
        self.assertEqual(str(parsed["volume"]), "10")
        self.assertIsNone(parsed["source_available_at"])
        source_clock = offline_minute_row({
            "ts_code": "600519.SH", "datetime": "2026-08-04 09:31:00",
            "source_available_at": "2026-08-04 09:31:05", "open": "1400", "high": "1402",
            "low": "1399", "close": "1401", "vol": "10",
        })
        self.assertEqual(source_clock["source_available_at"].isoformat(), "2026-08-04T01:31:05+00:00")
        with self.assertRaises(ValueError):
            offline_minute_row({"symbol": "600519.SH", "datetime": "2026-08-04 09:31:00", "open": "10", "high": "9", "low": "8", "close": "10"})
        with self.assertRaises(ValidationError):
            OfflineMinuteImportRequest(file_name="../outside.csv")

    def test_universe_and_provider_response_guards(self):
        universe = UniverseUpdateRequest(symbols=["603580.sh", "000636.SZ", "603580.SH"])
        self.assertEqual(universe.symbols, ["000636.SZ", "603580.SH"])
        with self.assertRaises(ValidationError):
            UniverseUpdateRequest(symbols=["艾艾精工"])
        self.assertTrue(looks_like_response_header([{"ts_code": "ts_code", "trade_date": "trade_date"}]))
        self.assertTrue(looks_like_response_header([
            {"ts_code": "ts_code", "trade_date": "trade_date"},
            {"ts_code": "ts_code", "trade_date": "trade_date"},
        ]))
        self.assertFalse(looks_like_response_header([{"ts_code": "603580.SH", "trade_date": "20260807"}]))
        self.assertFalse(realtime_rows_are_current("rt_k", [{"ts_code": "603580.SH", "trade_time": "20260807"}], date(2026, 8, 10)))
        self.assertTrue(realtime_rows_are_current("rt_k", [{"ts_code": "000636.SZ", "close": 58.26}], date(2026, 8, 10)))
        self.assertTrue(realtime_rows_are_current("rt_etf_k", [{"ts_code": "510300.SH", "close": 4.93}], date(2026, 8, 10)))
        self.assertTrue(realtime_rows_are_current("rt_min", [{"ts_code": "000636.SZ", "updated_at": "2026-08-10T10:16:36.000"}], date(2026, 8, 10)))
        self.assertTrue(realtime_rows_are_current("rt_min_daily", [{"ts_code": "000636.SZ", "time": "2026-08-10 15:00:00"}], date(2026, 8, 10)))
        china = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        observed_at = __import__("datetime").datetime(2026, 8, 11, 11, 20, tzinfo=china)
        self.assertTrue(realtime_rows_are_current(
            "rt_min", [{"ts_code": "600176.SH", "time": "2026-08-11 11:19:00"}], date(2026, 8, 11), observed_at,
        ))
        self.assertFalse(realtime_rows_are_current(
            "rt_min", [{"ts_code": "600176.SH", "time": "2026-08-11 15:00:00"}], date(2026, 8, 11), observed_at,
        ))
        self.assertFalse(realtime_rows_are_current(
            "rt_min_daily", [{"ts_code": "600176.SH", "time": "2026-08-11 15:00:00"}], date(2026, 8, 11), observed_at,
        ))
        self.assertFalse(realtime_rows_are_current(
            "rt_idx_min", [{"ts_code": "000001.SH", "time": "2026-08-11 15:00:00"}], date(2026, 8, 11), observed_at,
        ))
        self.assertTrue(realtime_rows_are_current("daily", [{"ts_code": "000636.SZ", "trade_date": "20260807"}], date(2026, 8, 10)))

    def test_technical_summary_requires_prices_and_exposes_trend_inputs(self):
        empty = technical_summary([])
        self.assertEqual(empty["status"], "insufficient_market_data")
        rows = [{"trade_date": f"202608{day:02d}", "close": 10 + day / 10} for day in range(1, 22)]
        summary = technical_summary(rows)
        self.assertEqual(summary["status"], "ready")
        self.assertIsNotNone(summary["sma_20"])

    def test_analyst_text_feature_projection_keeps_pit_window_and_empty_contract(self):
        class Cursor:
            def __init__(self):
                self.calls = []
                self.index = 0
            def execute(self, sql, params):
                self.calls.append((sql, params))
                self.index += 1
            def fetchall(self):
                return []
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        class Connection:
            def __init__(self): self.cursor_instance = Cursor()
            def cursor(self): return self.cursor_instance
        connection = Connection()
        payload = isolated_analyst_text_factor_summary(
            connection, date(2026, 8, 13), classify_text=lambda _text: (1, 1.0, 1.0),
            factor_version="test-v1", lookback_days=7,
        )
        self.assertEqual(payload["factor_version"], "test-v1")
        self.assertEqual(payload["market"]["report_count"], 0)
        self.assertEqual(payload["themes"], [])
        self.assertIn("Asia/Shanghai", connection.cursor_instance.calls[0][0])
        self.assertEqual(connection.cursor_instance.calls[0][1][0], date(2026, 8, 7))
