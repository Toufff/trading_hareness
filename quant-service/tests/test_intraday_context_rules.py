"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403


class IntradayContextRuleTests(unittest.TestCase):
    def test_intraday_attribution_separates_acceptance_market_and_sector_linkage(self):
        attribution = intraday_signal_attribution(
            "000001.SZ:entry:upside_acceptance_eac_v4", "entry",
            {"setup": "eac_acceptance_confirmed", "eac_acceptance_assessment": {"status": "candidate"}},
            {"peer_context": {"available_peer_count": 3, "confirming_peer_count": 2}},
            {"market_state": "rotation_defensive", "board_snapshot_age_seconds": 45,
             "symbol_board_matches": [{"label": "小金属", "net_inflow": 10}]},
        )
        self.assertEqual(attribution["model_version"], "eac-v4")
        self.assertEqual(attribution["stage"], "acceptance")
        self.assertEqual(attribution["market_state"], "rotation_defensive")
        self.assertEqual(attribution["sector_linkage"], "peer_and_board_top10_confirmed")
        self.assertEqual(attribution["volume_baseline"], "ready")

    def test_intraday_point_in_time_context_batch_uses_one_board_report_query(self):
        connection = MagicMock()
        first_report = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
        second_report = datetime(2026, 8, 10, 1, 5, tzinfo=timezone.utc)
        connection.execute.return_value.fetchall.return_value = [
            {"board_report_id": "first", "observed_at": first_report, "payload": {"items": []}},
            {"board_report_id": "second", "observed_at": second_report, "payload": {"items": []}},
        ]
        first_signal = datetime(2026, 8, 10, 1, 3, tzinfo=timezone.utc)
        second_signal = datetime(2026, 8, 10, 1, 7, tzinfo=timezone.utc)
        contexts = intraday_point_in_time_market_context_batch(
            connection, [(first_signal, "600000.SH"), (second_signal, "600000.SH"), (second_signal, "000001.SZ")],
        )
        self.assertEqual(connection.execute.call_count, 1)
        self.assertEqual(contexts[(first_signal, "600000.SH")]["board_report_id"], "first")
        self.assertEqual(contexts[(second_signal, "600000.SH")]["board_report_id"], "second")
        self.assertEqual(contexts[(second_signal, "000001.SZ")]["board_report_id"], "second")

    def test_intraday_scan_source_has_batched_board_and_paper_reads(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_scan_preparation.py").read_text(encoding="utf-8")
        scanner_source = (Path(__file__).resolve().parents[1] / "app" / "intraday_scan_signal_persistence.py").read_text(encoding="utf-8")
        repository_source = (Path(__file__).resolve().parents[1] / "app" / "intraday_scan_repository.py").read_text(encoding="utf-8")
        self.assertIn("market_contexts = dependencies.market_context_batch", source)
        self.assertIn("WHERE symbol=ANY(%s)", repository_source)
        self.assertIn("prepared.market_contexts.get((observed_at, symbol), {})", scanner_source)

    def test_intraday_scan_does_not_claim_tencent_completed_when_no_watch_quote_matches(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_scan_source_status.py").read_text(encoding="utf-8")
        self.assertIn('"completed" if fresh_direct_watch_count == len(selected_symbols)', source)
        self.assertIn('"decision_eligible_watch_quote_symbols": fresh_direct_watch_count', source)
        self.assertIn('"stale_or_unstamped_direct_watch_quote_symbols": direct_watch_count - fresh_direct_watch_count', source)

    def test_intraday_previous_quote_has_session_and_fifteen_second_freshness_bounds(self):
        preparation_source = (Path(__file__).resolve().parents[1] / "app" / "intraday_scan_preparation.py").read_text(encoding="utf-8")
        repository_source = (Path(__file__).resolve().parents[1] / "app" / "intraday_scan_repository.py").read_text(encoding="utf-8")
        self.assertIn("not_before=max(session_start, observed_at - timedelta(seconds=15))", preparation_source)
        self.assertIn("o.observed_at<%s AND o.observed_at>=%s", repository_source)
        self.assertIn("DISTINCT ON(o.symbol,o.source_name)", repository_source)
        self.assertIn("first_eac_by_symbol=dependencies.first_eac_events", preparation_source)

    def test_market_regime_runtime_uses_io_free_extracted_module(self):
        import app.main as main_module
        self.assertIs(main_module.strategy_rank, pure_strategy_rank)
        self.assertIs(main_module.strategy_market_regime, pure_strategy_market_regime)
        self.assertIs(main_module.strategy_market_state, pure_strategy_market_state)
        self.assertIs(main_module.strategy_index_regime, pure_strategy_index_regime)

    def test_intraday_number_runtime_uses_extracted_numeric_module(self):
        import app.main as main_module
        self.assertIs(main_module.intraday_number, pure_intraday_number)
        self.assertEqual(main_module.intraday_number("1,234.5%"), 1234.5)
        self.assertIsNone(main_module.intraday_number("—"))

    def test_decimal_normalizer_runtime_uses_extracted_numeric_module(self):
        import app.main as main_module

        self.assertIs(main_module.decimal_or_none, pure_decimal_or_none)
        self.assertEqual(main_module.decimal_or_none("1234.50"), Decimal("1234.50"))
        self.assertIsNone(main_module.decimal_or_none(""))

    def test_intraday_clock_runtime_uses_extracted_module(self):
        import app.main as main_module

        self.assertIs(main_module.intraday_feature_clock, pure_feature_clock)
        self.assertIs(main_module.intraday_eac_window, pure_eac_window)
        self.assertIs(main_module.intraday_minute_bucket, pure_minute_bucket)
        self.assertEqual(main_module.intraday_eac_window("2026-08-13 09:45:00"), "morning")
        self.assertEqual(main_module.intraday_minute_bucket("13:05"), "13:05")

    def test_intraday_features_runtime_uses_extracted_module(self):
        import app.main as main_module

        self.assertIsNot(main_module.intraday_minute_features, pure_minute_features)
        self.assertIsNot(main_module.intraday_peer_context, pure_peer_context)
        self.assertEqual(main_module.intraday_peer_context([], {}), pure_peer_context([], {}))

    def test_limit_daily_features_runtime_uses_extracted_module(self):
        import app.main as main_module

        bars = [{"trading_date": "20260812", "close": "10", "volume": "100"},
                {"trading_date": "20260813", "pre_close": "10", "open": "10", "high": "11", "low": "9", "close": "11", "volume": "200", "symbol": "000001.SZ"}]
        self.assertEqual(main_module.post_close_limit_daily_features(bars), pure_limit_daily_features(
            bars, number=main_module.intraday_number, limit_ratio=main_module.a_share_limit_ratio,
        ))
        self.assertEqual(main_module.limit_board_count("8天6板"), pure_board_count("8天6板"))
        rows = [{"time": "09:30", "close": "10"}, {"time": "09:30", "close": "11"},
                {"time": "12:00", "close": "12"}, {"time": "13:01", "close": "13"}]
        self.assertEqual(main_module._strategy_session_rows(rows), pure_strategy_session_rows(rows, number=main_module.intraday_number))

    def test_intraday_attribution_summary_keeps_small_cohorts_descriptive(self):
        observed = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        base = {"horizon_key": "5m", "status": "matured", "observed_at": observed,
                "maximum_favorable_excursion": 0.02, "maximum_adverse_excursion": -0.01,
                "attribution": {"model_version": "eac-v4", "stage": "acceptance",
                                "market_state": "rotation_defensive", "sector_linkage": "peer_confirmed",
                                "volume_baseline": "ready"}}
        result = intraday_outcome_attribution_summary([
            {**base, "signal_event_id": "one", "raw_return": 0.01},
            {**base, "signal_event_id": "two", "raw_return": -0.005},
        ])
        stage = next(item for item in result["items"] if item["dimension"] == "stage")
        self.assertEqual(stage["matured"], 2)
        self.assertEqual(stage["hit_rate"], 0.5)
        self.assertEqual(stage["evaluation_status"], "descriptive_only")
        self.assertEqual(result["validation_gate"]["matured_unique_signals"], 2)
        self.assertEqual(result["validation_gate"]["status"], "accumulating")
