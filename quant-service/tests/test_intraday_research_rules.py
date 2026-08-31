"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403


class IntradayResearchRuleTests(unittest.TestCase):
    def test_smart_money_q_uses_the_same_rolling_window_vwap_and_volume_share(self):
        rows = []
        for index in range(30):
            close = 10.0 + index * 0.01
            volume = 100 if index < 29 else 1000
            rows.append({"time": f"10{index:02d}", "close": close, "volume_lot": volume,
                         "amount": close * volume * 100, "vwap": 9.0})
        features = intraday_minute_features(rows)
        self.assertIsNotNone(features)
        self.assertGreaterEqual(features["smart_money_selected_volume_share_30m"], 0.2)
        self.assertNotEqual(features["smart_money_window_vwap_30m"], 9.0)

    def test_outcome_decomposition_keeps_overnight_separate_from_intraday_return(self):
        result = a_share_return_decomposition(Decimal("10"), 1, Decimal("10.5"), Decimal("10.2"), Decimal("10.8"))
        self.assertEqual(result["trigger_to_close"], Decimal("0.05"))
        self.assertEqual(result["overnight"], Decimal("-0.0285714285714285714285714286"))
        self.assertAlmostEqual(float(result["next_day_intraday"] or 0), 0.0588235294117647059, places=12)
        self.assertEqual(result["trigger_to_next_close"], Decimal("0.08"))

    def test_intraday_attribution_labels_microstructure_as_observational(self):
        attribution = intraday_signal_attribution(
            "000001.SZ:watch:test", "watch", {},
            {"tencent_order_book": {"status": "observed", "latest_features": {"status": "observed", "delta_status": "ready", "qi5": 0.4},
                                      "ofi_30s": 3, "ofi_30s_sample_count": 3},
             "tencent_minute": {"price_log_volume_corr_30m": -0.4, "smart_money_q_30m": 0.99}},
        )
        self.assertEqual(attribution["microstructure_state"], "observed_bid_heavy_positive_ofi_30s")
        self.assertEqual(attribution["ofi_attribution_window"], "30s")
        self.assertEqual(attribution["price_volume_state"], "negative_corr")
        self.assertEqual(attribution["smart_money_state"], "below_session_vwap")

    def test_extracted_intraday_modules_match_compatibility_exports(self):
        evidence = {"tencent_order_book": {"status": "observed", "latest_features": {"status": "observed", "delta_status": "ready", "qi5": 0.4}, "ofi_30s": 3, "ofi_30s_sample_count": 3}}
        self.assertEqual(
            intraday_signal_attribution("000001.SZ:watch:test", "watch", {}, evidence),
            isolated_signal_attribution("000001.SZ:watch:test", "watch", {}, evidence,
                                        number=pure_intraday_number, signal_model_version="watchlist-confirmation-v6"),
        )
        watch = {"symbol": "000001.SZ", "entry_price": None, "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True}
        quote = {"price": 10.2, "pct_change": 2.0, "volume_ratio": 2.0, "turnover_rate": 4.0, "main_net_inflow": 100, "main_flow_percentile": 0.95}
        self.assertEqual(
            intraday_signal_rules(watch, quote, {"price": 10.1}),
            isolated_signal_rules(watch, quote, {"price": 10.1}, number=pure_intraday_number,
                                   upside_assessment_fn=lambda q, d, m, p: isolated_upside_assessment(q, d, m, p, number=pure_intraday_number, eac_window=pure_eac_window),
                                   model_version="watchlist-confirmation-v6"),
        )
        items = [{"signal_event_id": "s1", "status": "matured", "raw_return": 0.01,
                  "maximum_favorable_excursion": 0.02, "maximum_adverse_excursion": -0.005,
                  "observed_at": datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
                  "attribution": {"model_version": "v1"}, "horizon_key": "5m"}]
        self.assertEqual(intraday_outcome_attribution_summary(items), isolated_outcome_attribution_summary(items, number=pure_intraday_number))

    def test_green_reclaim_research_rule_requires_price_volume_vwap_and_flow_confirmation(self):
        rows = []
        for index in range(25):
            close = 10.0
            if index == 4:
                close = 9.8
            if index == 22:
                close = 10.0
            if index == 23:
                close = 10.1
            if index == 24:
                close = 10.2
            rows.append({"time": f"10{index:02d}", "close": close, "volume_lot": 4 if index == 24 else 1,
                         "amount": close * (400 if index == 24 else 100), "vwap": 9.95})
        feature = intraday_minute_features(rows)
        self.assertIsNotNone(feature)
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None, "alert_on_entry": True,
                 "alert_on_exit": True, "metadata": {"reversal_research": {"enabled": True, "label": "research"}}}
        quote = {"price": 10.2, "pct_change": 2.0, "volume_ratio": 2.0, "turnover_rate": 4.0,
                 "main_net_inflow": 100, "main_flow_percentile": 0.95}
        previous = {"price": 9.98, "pct_change": -0.2}
        signals = intraday_signal_rules(watch, quote, previous, None, feature, {"available_peer_count": 0})
        self.assertEqual(signals[0]["signal_key"], "000001.SZ:watch:green_reclaim_research_v1")

    def test_fuyao_minute_breadth_entry_replaces_missing_public_flow_fields(self):
        watch = {
            "symbol": "000001.SZ", "available_quantity": 0, "entry_price": None,
            "alert_on_entry": True, "alert_on_exit": True,
        }
        quote = {
            "price": 10.4, "pct_change": 2.4,
            "price_source": "tencent_batched_watch_quote",
            "price_freshness": {"status": "fresh"},
        }
        minute = {
            "return_1m_pct": 0.9, "return_3m_pct": 1.8,
            "minute_volume_multiple": 3.4, "above_vwap_pct": 1.2,
        }
        peers = {"available_peer_count": 3, "confirming_peer_count": 2, "confirming_breadth": 0.67}
        signals = intraday_signal_rules(watch, quote, {"price": 10.3}, None, minute, peers)
        entry = next(item for item in signals if item["signal_key"] == "000001.SZ:entry:fuyao_minute_breadth_v1")
        self.assertTrue(entry["independent_confirmation"])
        self.assertEqual(entry["conditions"]["flow_confirmation"], "not_required_fuyao_no_flow_semantics")
        suppressed = intraday_signal_rules(
            watch, quote, {"price": 10.3}, None, minute,
            {"available_peer_count": 3, "confirming_peer_count": 1, "confirming_breadth": 0.33},
        )
        self.assertFalse(any(item["signal_key"].endswith(":entry:fuyao_minute_breadth_v1") for item in suppressed))

    def test_eastmoney_watch_flow_is_evidence_only_and_cannot_reenable_legacy_entry(self):
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None,
                 "alert_on_entry": True, "alert_on_exit": True}
        quote = {
            "price": 10.4, "pct_change": 2.4, "volume_ratio": 3.5, "turnover_rate": 6.0,
            "main_net_inflow": 1000, "price_source": "tencent_batched_watch_quote",
            "price_freshness": {"status": "fresh"},
            "flow_snapshot": {"scope": "explicit_watchlist_only", "cross_sectional": False,
                              "decision_eligible": False},
        }
        minute = {"return_1m_pct": 0.9, "return_3m_pct": 1.8,
                  "minute_volume_multiple": 3.4, "above_vwap_pct": 1.2}
        peers = {"available_peer_count": 3, "confirming_peer_count": 2, "confirming_breadth": 0.67}
        signals = intraday_signal_rules(watch, quote, {"price": 10.3}, None, minute, peers)
        self.assertFalse(any(item["signal_key"] == "000001.SZ:entry:intraday-v1" for item in signals))
        entry = next(item for item in signals if item["signal_key"] == "000001.SZ:entry:fuyao_minute_breadth_v1")
        self.assertEqual(entry["conditions"]["flow_confirmation"], "eastmoney_watch_flow_observed_research_only")
        self.assertIn("eastmoney_watch_flow_research_confirmation_only", entry["risk_flags"])

    def test_upside_breakout_research_requires_causal_high_volume_vwap_and_flow(self):
        rows = []
        prices = [10.0] * 20 + [10.05, 10.10, 10.25, 10.40]
        for index, close in enumerate(prices):
            volume = 4 if index == len(prices) - 1 else 1
            rows.append({"time": f"10{index:02d}", "close": close, "volume_lot": volume,
                         "amount": close * volume * 100, "vwap": 9.99})
        feature = intraday_minute_features(rows)
        self.assertGreater(feature["breakout_above_prior_high_pct"], 0)
        self.assertGreaterEqual(feature["session_range_position"], 0.99)
        feature["time_bucket_volume_profile"] = {"status": "ready", "sample_days": 8,
                                                  "median_volume": 1, "volume_surprise": 4}
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None, "alert_on_entry": True,
                 "alert_on_exit": True, "metadata": {"upside_research": {"enabled": True, "label": "breakout"}}}
        quote = {"price": 10.4, "pct_change": 4.0, "volume_ratio": 1.8, "turnover_rate": 4.0,
                 "main_net_inflow": 100, "main_flow_percentile": 0.9}
        daily = {"status": "completed", "ma_trend": "bullish"}
        signals = intraday_signal_rules(watch, quote, {"price": 10.25}, daily, feature, {"available_peer_count": 0})
        self.assertEqual(signals[0]["signal_key"], "000001.SZ:watch:upside_breakout_eac_v3")
        self.assertEqual(signals[0]["conditions"]["upside_research_assessment"]["status"], "candidate")
        quote["pct_change"] = 6.6
        self.assertFalse(any(item["signal_key"] == "000001.SZ:watch:upside_breakout_eac_v3"
                             for item in intraday_signal_rules(watch, quote, {"price": 10.25}, daily, feature, {})))

    def test_eac_marks_extreme_minute_volume_as_attention_not_candidate(self):
        rows = []
        prices = [10.0] * 20 + [10.05, 10.10, 10.25, 10.40]
        for index, close in enumerate(prices):
            volume = 30 if index == len(prices) - 1 else 1
            rows.append({"time": f"10:{index:02d}", "close": close, "volume_lot": volume,
                         "amount": close * volume * 100, "vwap": 9.99})
        feature = intraday_minute_features(rows)
        feature["time_bucket_volume_profile"] = {"status": "ready", "sample_days": 8,
                                                  "median_volume": 1, "volume_surprise": 30}
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None, "alert_on_entry": True,
                 "alert_on_exit": True, "metadata": {"upside_research": {"enabled": True}}}
        quote = {"price": 10.4, "pct_change": 4.0, "volume_ratio": 1.8, "turnover_rate": 4.0,
                 "main_net_inflow": 100, "main_flow_percentile": 0.9}
        signals = intraday_signal_rules(watch, quote, {"price": 10.25}, {"ma_trend": "bullish"}, feature, {})
        self.assertEqual(signals[0]["conditions"]["eac_state"], "attention_only")
        self.assertIn("relative_volume_outlier_requires_acceptance", signals[0]["risk_flags"])

    def test_eac_without_same_clock_history_is_attention_only(self):
        rows = []
        prices = [10.0] * 20 + [10.05, 10.10, 10.25, 10.40]
        for index, close in enumerate(prices):
            volume = 4 if index == len(prices) - 1 else 1
            rows.append({"time": f"10:{index:02d}", "close": close, "volume_lot": volume,
                         "amount": close * volume * 100, "vwap": 9.99})
        feature = intraday_minute_features(rows)
        watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None, "alert_on_entry": True,
                 "alert_on_exit": True, "metadata": {"upside_research": {"enabled": True}}}
        quote = {"price": 10.4, "pct_change": 4.0, "volume_ratio": 1.8, "turnover_rate": 4.0,
                 "main_net_inflow": 100, "main_flow_percentile": 0.9}
        signals = intraday_signal_rules(watch, quote, {"price": 10.25}, {"ma_trend": "bullish"}, feature, {})
        assessment = signals[0]["conditions"]["upside_research_assessment"]
        self.assertEqual(assessment["status"], "attention_only")
        self.assertIn("time_bucket_volume_baseline_insufficient", signals[0]["risk_flags"])

    def test_eac_acceptance_requires_time_price_vwap_and_range_retention(self):
        first_at = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        first_conditions = {"price": 10.40, "upside_research_assessment": {"status": "candidate"}}
        quote = {"price": 10.38, "pct_change": 3.8, "main_flow_percentile": 0.9}
        minute = {"above_vwap_pct": 1.2, "session_range_position": 0.88}
        accepted = intraday_eac_acceptance_assessment(
            first_conditions, first_observed_at=first_at,
            observed_at=first_at + __import__("datetime").timedelta(seconds=40),
            quote=quote, previous_quote={"price": 10.39}, minute_features=minute,
            peer_context={"available_peer_count": 0},
        )
        self.assertEqual(accepted["status"], "candidate")
        early = intraday_eac_acceptance_assessment(
            first_conditions, first_observed_at=first_at,
            observed_at=first_at + __import__("datetime").timedelta(seconds=10),
            quote=quote, previous_quote={"price": 10.39}, minute_features=minute,
            peer_context={"available_peer_count": 0},
        )
        self.assertEqual(early["status"], "not_confirmed")
        attention = intraday_eac_acceptance_assessment(
            {"price": 10.40, "upside_research_assessment": {"status": "attention_only"}},
            first_observed_at=first_at, observed_at=first_at + __import__("datetime").timedelta(seconds=40),
            quote=quote, previous_quote={"price": 10.39}, minute_features=minute,
            peer_context={"available_peer_count": 0},
        )
        self.assertEqual(attention["status"], "attention_only")

    def test_multi_index_regime_calls_a_rebound_corrective_without_inferring_wave_count(self):
        rows = []
        prices = [100, 98, 96, 94, 92, 90, 87, 84, 82, 80,
                  81, 82, 83, 84, 85, 86, 87, 88, 89, 90]
        for symbol in ("000001.SH", "000300.SH", "399001.SZ", "399006.SZ"):
            for index, close in enumerate(prices, start=1):
                rows.append({"symbol": symbol, "trading_date": date(2026, 7, index),
                             "high": close + 1, "low": close - 1, "close": close, "volume": 100})
        regime = strategy_index_regime(rows)
        self.assertEqual(regime["state"], "corrective_rebound")
        self.assertEqual(regime["index_count"], 4)
        self.assertEqual(regime["interpretation"], "B-wave is an analyst scenario label only")


class PerFieldFlowTrustTests(unittest.TestCase):
    """The licensed derived metrics are trusted per field; Eastmoney is not.

    ``volume_ratio`` and ``turnover_rate`` derived from the licensed all-A
    snapshot are cross-sectional definitions, so they are usable on their own
    terms.  ``main_net_inflow`` has no licensed source and stays research-only,
    which is what keeps the legacy entry rule gated.
    """

    watch = {"symbol": "000001.SZ", "available_quantity": 0, "entry_price": None,
             "alert_on_entry": True, "alert_on_exit": True}

    def _quote(self, sources, **overrides):
        return {"price": 10.4, "pct_change": 2.4, "volume_ratio": 3.5, "turnover_rate": 6.0,
                "main_net_inflow": 1000, "price_source": "tencent_batched_watch_quote",
                "price_freshness": {"status": "fresh"},
                "flow_snapshot": {"scope": "explicit_watchlist_only", "cross_sectional": False,
                                  "decision_eligible": False},
                "flow_metric_sources": sources, **overrides}

    def test_derived_volume_fields_are_usable_while_main_flow_stays_research_only(self):
        quote = self._quote({"volume_ratio": "fuyao_ths_derived", "turnover_rate": "fuyao_ths_derived",
                             "main_net_inflow": "eastmoney_watch_flow"})
        signals = intraday_signal_rules(self.watch, quote, {"price": 10.3}, None, None, None)
        availability = signals[0]["conditions"]["data_availability"]
        self.assertEqual(availability["missing_public_flow_fields"], ["main_net_inflow"])
        self.assertFalse(availability["public_flow_available"])
        self.assertFalse(any(item["signal_key"] == "000001.SZ:entry:intraday-v1" for item in signals),
                         "entry_setup still requires a main_net_inflow nobody licenses")

    def test_derived_volume_fields_revive_the_volume_anomaly_watch(self):
        quote = self._quote({"volume_ratio": "fuyao_ths_derived", "turnover_rate": "fuyao_ths_derived",
                             "main_net_inflow": "unavailable"}, main_net_inflow=None)
        signals = intraday_signal_rules(self.watch, quote, {"price": 10.3}, None, None, None)
        anomaly = next(item for item in signals if item["signal_key"] == "000001.SZ:watch:volume_anomaly")
        self.assertEqual(anomaly["conditions"]["anomaly_direction"], "up")
        # Without adaptive metadata the gates are exactly the historical floors.
        self.assertEqual(anomaly["conditions"]["volume_ratio_gate"], 2.5)
        self.assertEqual(anomaly["conditions"]["turnover_rate_gate"], 5.0)

    _trusted = {"volume_ratio": "fuyao_ths_derived", "turnover_rate": "fuyao_ths_derived",
                "main_net_inflow": "unavailable"}

    def test_adaptive_percentiles_raise_the_anomaly_bar_for_active_names(self):
        # A chronically active name: turnover 6% and volume ratio 3.5 clear the
        # absolute floors, but sit below its own rolling percentiles, so what
        # used to fire every few minutes is no longer anomalous.
        watch = {**self.watch, "metadata": {"volume_anomaly_thresholds": {
            "volume_ratio_p95": 4.1, "turnover_rate_p90": 8.0}}}
        quote = self._quote(self._trusted, main_net_inflow=None)
        signals = intraday_signal_rules(watch, quote, {"price": 10.3}, None, None, None)
        self.assertFalse(any(item["signal_key"] == "000001.SZ:watch:volume_anomaly" for item in signals))

    def test_adaptive_percentiles_still_fire_on_a_genuine_outlier(self):
        watch = {**self.watch, "metadata": {"volume_anomaly_thresholds": {
            "volume_ratio_p95": 4.1, "turnover_rate_p90": 8.0}}}
        quote = self._quote(self._trusted, main_net_inflow=None, volume_ratio=4.6, turnover_rate=9.2)
        signals = intraday_signal_rules(watch, quote, {"price": 10.3}, None, None, None)
        anomaly = next(item for item in signals if item["signal_key"] == "000001.SZ:watch:volume_anomaly")
        self.assertEqual(anomaly["conditions"]["volume_ratio_gate"], 4.1)
        self.assertEqual(anomaly["conditions"]["turnover_rate_gate"], 8.0)

    def test_tiny_percentiles_never_lower_the_absolute_floors(self):
        # A quiet name's own P95 can be far below the floors; the floors win so
        # adaptive thresholds only ever reduce noise, never add it.
        watch = {**self.watch, "metadata": {"volume_anomaly_thresholds": {
            "volume_ratio_p95": 0.9, "turnover_rate_p90": 1.2}}}
        quote = self._quote(self._trusted, main_net_inflow=None, volume_ratio=2.6, turnover_rate=5.5)
        signals = intraday_signal_rules(watch, quote, {"price": 10.3}, None, None, None)
        anomaly = next(item for item in signals if item["signal_key"] == "000001.SZ:watch:volume_anomaly")
        self.assertEqual(anomaly["conditions"]["volume_ratio_gate"], 2.5)
        self.assertEqual(anomaly["conditions"]["turnover_rate_gate"], 5.0)

    # Minute/peer inputs that satisfy the fuyao_minute_breadth entry, which is
    # the only rule that fires when every public flow field is research-only -
    # without them a fully zeroed quote produces no signal to inspect at all.
    minute = {"return_1m_pct": 0.9, "return_3m_pct": 1.8,
              "minute_volume_multiple": 3.4, "above_vwap_pct": 1.2}
    peers = {"available_peer_count": 3, "confirming_peer_count": 2, "confirming_breadth": 0.67}

    def test_eastmoney_labelled_fields_stay_zeroed_exactly_as_before(self):
        quote = self._quote({"volume_ratio": "eastmoney_watch_flow", "turnover_rate": "eastmoney_watch_flow",
                             "main_net_inflow": "eastmoney_watch_flow"})
        signals = intraday_signal_rules(self.watch, quote, {"price": 10.3}, None, self.minute, self.peers)
        entry = next(item for item in signals if item["signal_key"] == "000001.SZ:entry:fuyao_minute_breadth_v1")
        self.assertEqual(entry["conditions"]["data_availability"]["missing_public_flow_fields"],
                         ["volume_ratio", "turnover_rate", "main_net_inflow"])
        self.assertFalse(any(item["signal_key"] == "000001.SZ:watch:volume_anomaly" for item in signals))

    def test_snapshots_frozen_before_labelling_keep_the_original_behaviour(self):
        quote = self._quote({})
        del quote["flow_metric_sources"]
        signals = intraday_signal_rules(self.watch, quote, {"price": 10.3}, None, self.minute, self.peers)
        entry = next(item for item in signals if item["signal_key"] == "000001.SZ:entry:fuyao_minute_breadth_v1")
        availability = entry["conditions"]["data_availability"]
        self.assertEqual(availability["missing_public_flow_fields"],
                         ["volume_ratio", "turnover_rate", "main_net_inflow"])
        self.assertIsNone(availability["flow_metric_sources"])
        self.assertTrue(availability["eastmoney_watch_flow_observed_research_only"])

    def test_a_fully_research_only_quote_with_no_minute_evidence_fires_nothing(self):
        """Documents the state this change fixes: every flow rule is dead."""
        quote = self._quote({"volume_ratio": "eastmoney_watch_flow", "turnover_rate": "eastmoney_watch_flow",
                             "main_net_inflow": "eastmoney_watch_flow"})
        self.assertEqual(intraday_signal_rules(self.watch, quote, {"price": 10.3}, None, None, None), [])
