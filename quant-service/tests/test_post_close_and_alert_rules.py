"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403


class PostCloseAndAlertRuleTests(unittest.TestCase):
    def test_post_close_15_session_structures_are_explicitly_provisional(self):
        bars = []
        for index in range(15):
            close = 10.0 + (index % 3) * 0.05
            bars.append({"high": close + 0.08, "low": close - 0.08, "close": close,
                         "volume": 100 if index < 12 else 60, "adj_factor": 1.0})
        forming = post_close_forming_structure(bars)
        self.assertIn(forming["status"], {"forming", "not_ready"})
        self.assertEqual(forming["bar_count"], 15)
        self.assertIn("15日", forming["notice"])

        fresh = [dict(item) for item in bars]
        fresh[-6]["close"] = 10.0
        fresh[-4]["close"] = 10.15
        fresh[-2]["close"] = 10.25
        fresh[-1].update({"close": 10.62, "high": 10.70, "low": 10.50, "volume": 220})
        started = post_close_fresh_start_structure(fresh)
        self.assertEqual(started["status"], "started")
        self.assertGreaterEqual(started["metrics"]["volume_multiple_5d"], 1.5)

    def test_post_close_structures_refuse_mixed_adjustment_basis(self):
        bars = [{"high": 10.2, "low": 9.8, "close": 10.0, "volume": 100, "adj_factor": 1.0}
                for _ in range(30)]
        bars[-1].pop("adj_factor")
        result = daily_base_structure(bars)
        self.assertEqual(result["status"], "data_quality_blocked")
        self.assertIn("adj_factor_missing", result["quality_flags"])

    def test_post_close_candidate_lifecycle_contract_is_persisted(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "post_close_strategy_service.py").read_text(encoding="utf-8")
        self.assertIn("discovered_at,expires_at,reason_codes,source_snapshot", source)
        self.assertIn("as_of_date + timedelta(days=1)", source)

    def test_post_close_read_model_keeps_blocked_attempt_separate_from_last_completed(self):
        connection = MagicMock()
        attempt = {"run_id": "attempt", "as_of_date": date(2026, 8, 13), "status": "blocked"}
        completed = {"run_id": "completed", "as_of_date": date(2026, 8, 12), "status": "completed"}
        connection.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=attempt)),
            MagicMock(fetchone=MagicMock(return_value=completed)),
            MagicMock(fetchall=MagicMock(return_value=[{"symbol": "000636.SZ", "rank": 1}])),
        ]
        database = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        result = read_latest_post_close_strategy(database)
        self.assertEqual(result["latest_attempt"]["status"], "blocked")
        self.assertEqual(result["run"]["status"], "blocked")
        self.assertEqual(result["run"]["as_of_date"], date(2026, 8, 13))
        self.assertEqual(result["latest_completed"]["status"], "completed")
        self.assertEqual(result["candidate_run"]["as_of_date"], date(2026, 8, 12))
        self.assertEqual(result["candidates"][0]["symbol"], "000636.SZ")

    def test_limit_ladder_and_ground_to_sky_replay_keep_causal_checkpoints(self):
        self.assertEqual(limit_board_count("首板"), 1)
        self.assertEqual(limit_board_count("8天6板"), 6)
        daily = post_close_limit_daily_features([
            {"trading_date": date(2026, 8, 10), "open": 11.57, "high": 11.57, "low": 11.57,
             "close": 11.57, "pre_close": 12.85, "volume": 26000, "selected_provider": "super_get"},
            {"trading_date": date(2026, 8, 11), "open": 12.0, "high": 12.73, "low": 10.41,
             "close": 12.73, "pre_close": 11.57, "volume": 1340000, "selected_provider": "super_get"},
        ])
        self.assertTrue(daily["ground_to_sky_daily_shape"])
        prices = [12.0, 11.2, 10.6, 10.41, 10.41, 10.45, 10.46, 10.50, 10.55, 10.57,
                  10.78, 10.89, 11.25, 11.10, 11.49, 11.96, 12.19, 11.70, 12.14, 12.73]
        times = [f"09{30 + index:02d}" for index in range(10)] + [f"13{index:02d}" for index in range(10)]
        volumes = [1000] * 10 + [4500, 5000, 4200, 1800, 2200, 4000, 3500, 2000, 2600, 6000]
        cumulative_volume = 0
        cumulative_amount = 0.0
        rows = []
        for minute, price, volume in zip(times, prices, volumes, strict=True):
            cumulative_volume += volume
            cumulative_amount += price * volume * 100
            rows.append({"time": minute, "close": price, "volume_lot": volume,
                         "amount": price * volume * 100, "cumulative_volume_lot": cumulative_volume,
                         "cumulative_amount": cumulative_amount,
                         "vwap": cumulative_amount / (cumulative_volume * 100), "is_complete": True})
        pattern = intraday_limit_lift_pattern(rows, daily)
        self.assertEqual(pattern["status"], "completed")
        self.assertIn("ground_to_sky_reversal", pattern["pattern_tags"])
        self.assertIsNotNone(pattern["deep_reversal_impulse"])
        self.assertIsNotNone(pattern["previous_close_reclaim"])
        self.assertIsNotNone(pattern["previous_close_acceptance"])
        self.assertEqual(pattern["limit_reclaim"]["time"], "1309")

    def test_deep_reversal_research_alerts_impulse_then_previous_close_acceptance(self):
        watch = {"symbol": "000779.SZ", "entry_price": None, "available_quantity": 0,
                 "alert_on_entry": True, "alert_on_exit": False,
                 "metadata": {"reversal_research": {"enabled": True, "label": "ground_to_sky"}}}
        impulse_quote = {"price": 9.4, "pct_change": -6.0, "volume_ratio": 3, "turnover_rate": 12,
                         "main_net_inflow": 10, "main_flow_percentile": 0.95}
        impulse_minute = {"return_1m_pct": 0.8, "return_3m_pct": 2.0, "minute_volume_multiple": 3.5,
                          "above_vwap_pct": 0.5, "recovery_from_session_low_pct": 4.4,
                          "session_low_price": 9.0}
        impulse = intraday_signal_rules(watch, impulse_quote, {"price": 9.2}, None, impulse_minute, {})
        self.assertEqual(impulse[0]["signal_key"], "000779.SZ:watch:deep_reversal_impulse_v1")
        reclaim_quote = {"price": 10.1, "pct_change": 1.0, "volume_ratio": 3, "turnover_rate": 18,
                         "main_net_inflow": 20, "main_flow_percentile": 0.96}
        reclaim_minute = {"return_1m_pct": 0.6, "return_3m_pct": 1.0, "minute_volume_multiple": 2.0,
                          "above_vwap_pct": 3.0, "recovery_from_session_low_pct": 12.2,
                          "session_low_price": 9.0}
        reclaim = intraday_signal_rules(watch, reclaim_quote, {"price": 9.9}, None, reclaim_minute, {})
        self.assertEqual(reclaim[0]["signal_key"], "000779.SZ:watch:deep_reversal_previous_close_acceptance_v1")
        self.assertTrue(reclaim[0]["stage_upgrade"])

    def test_opening_ladder_drive_uses_price_checkpoints_not_sealed_board_volume(self):
        daily = {"pre_close": 9.53, "close": 10.48, "close_pct": 9.97}
        prices = [9.56, 9.65, 9.78, 9.95, 10.02, 10.10, 10.32, 10.38, 10.43, 10.48,
                  10.48, 10.48, 10.48, 10.48, 10.48, 10.48]
        volumes = [8000, 9000, 10000, 11000, 13000, 12000, 15000, 18000, 22000, 40000,
                   100, 100, 100, 100, 50000, 100]
        rows = [{"time": f"09{30 + index:02d}", "close": price, "volume_lot": volume}
                for index, (price, volume) in enumerate(zip(prices, volumes, strict=True))]
        pattern = intraday_limit_lift_pattern(rows, daily)
        self.assertIn("opening_ladder_drive", pattern["pattern_tags"])
        self.assertEqual(pattern["opening_drive"]["first_four_pct_time"], "0933")
        self.assertEqual(pattern["opening_drive"]["first_eight_pct_time"], "0936")
        self.assertEqual(pattern["limit_reclaim"]["time"], "0939")
        self.assertGreaterEqual(pattern["post_limit_volume_spike_minutes"], 1)

    def test_daily_deep_low_without_minute_close_extreme_stays_unconfirmed(self):
        daily = {"pre_close": 14.48, "close": 15.93, "close_pct": 10.01, "low_pct": -9.53}
        prices = [14.0, 13.77, 13.57, 13.42, 13.71, 13.58, 13.82, 14.09, 14.18, 14.0,
                  14.16, 14.45, 14.9, 15.11, 15.42, 15.93]
        rows = [{"time": f"09{30 + index:02d}", "close": price, "volume_lot": 1000 + index * 100}
                for index, price in enumerate(prices)]
        pattern = intraday_limit_lift_pattern(rows, daily)
        self.assertIn("intraminute_extreme_not_in_minute_close", pattern["pattern_tags"])
        self.assertEqual(pattern["deep_discount_stabilization"]["time"], "0936")
        self.assertEqual(pattern["deep_discount_stabilization"]["confirmation"], "price_only_unconfirmed")
        self.assertIsNone(pattern["deep_reversal_impulse"])

    def test_eac_first_observation_alerts_before_second_scan(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        signal = {"signal_type": "watch", "hard": False, "alert_on_first_observation": True}
        self.assertEqual(intraday_signal_event_state(
            signal, observed_at=now, latest_event_at=None, last_key_alerted_at=None, last_symbol_watch_alerted_at=None,
        ), "confirmed")
