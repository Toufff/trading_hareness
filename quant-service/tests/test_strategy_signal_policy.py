"""Focused regression tests extracted from the legacy provider helper suite."""

from provider_test_support import *  # noqa: F403


class StrategySignalPolicyTests(unittest.TestCase):
    def test_post_close_base_contraction_requires_a_ready_but_unbroken_range(self):
        closes = [9.7, 10.25, 9.8, 10.2, 9.75, 10.3, 9.85, 10.2, 9.9, 10.25,
                  9.8, 10.3, 9.9, 10.2, 10.0, 10.05, 10.10, 10.08, 10.12, 10.10,
                  10.14, 10.12, 10.16, 10.13, 10.15, 10.14, 10.18, 10.16, 10.20, 10.28]
        bars = []
        for index, close in enumerate(closes):
            narrow = index >= 15
            bars.append({"close": close, "high": 10.4 if narrow else close + 0.2,
                         "low": 9.6 if narrow and index % 5 == 0 else close - (0.08 if narrow else 0.2),
                         "volume": 50 if index >= 25 else 70 if narrow else 120, "adj_factor": 1.0})
        structure = daily_base_structure(bars)
        self.assertEqual(structure["status"], "ready")
        self.assertTrue(structure["components"]["volume_dry_up"])
        self.assertTrue(structure["components"]["near_resistance"])
        self.assertLess(structure["metrics"]["close_to_resistance_pct"], 3.0)

    def test_intraday_alert_cooldown_uses_last_alert_not_last_suppressed_event(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)
        watch = {"signal_type": "watch", "hard": False}
        self.assertEqual(intraday_signal_event_state(
            watch, observed_at=now, latest_event_at=now - timedelta(seconds=30),
            last_key_alerted_at=now - timedelta(minutes=1), last_symbol_watch_alerted_at=now - timedelta(minutes=1),
        ), "suppressed")
        # A different watch key is also suppressed while a recent watch for the
        # same symbol is cooling down, but a true entry remains eligible.
        self.assertEqual(intraday_signal_event_state(
            watch, observed_at=now, latest_event_at=None, last_key_alerted_at=None,
            last_symbol_watch_alerted_at=now - timedelta(minutes=1),
        ), "suppressed")
        stage_upgrade = {"signal_type": "watch", "hard": False, "independent_confirmation": True,
                         "stage_upgrade": True}
        self.assertEqual(intraday_signal_event_state(
            stage_upgrade, observed_at=now, latest_event_at=None, last_key_alerted_at=None,
            last_symbol_watch_alerted_at=now - timedelta(minutes=1),
        ), "confirmed")
        entry = {"signal_type": "entry", "hard": False, "independent_confirmation": True}
        self.assertEqual(intraday_signal_event_state(
            entry, observed_at=now, latest_event_at=None, last_key_alerted_at=None,
            last_symbol_watch_alerted_at=now - timedelta(minutes=1),
        ), "confirmed")

    def test_intraday_same_episode_realerts_only_after_material_change(self):
        now = datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)
        signal = {"signal_type": "watch", "hard": False, "score": 70,
                  "conditions": {"price": 10.0, "volume_ratio": 2.0, "main_net_inflow": 100}}
        prior = {"score": 65, "conditions": {"price": 10.0, "volume_ratio": 2.0, "main_net_inflow": 100}}
        self.assertEqual(intraday_signal_event_state(
            signal, observed_at=now, latest_event_at=now - timedelta(seconds=30),
            last_key_alerted_at=now - timedelta(minutes=20), last_symbol_watch_alerted_at=None,
            last_key_alert=prior,
        ), "suppressed")
        signal["conditions"] = {**signal["conditions"], "price": 10.2}
        self.assertEqual(intraday_signal_event_state(
            signal, observed_at=now, latest_event_at=now - timedelta(seconds=30),
            last_key_alerted_at=now - timedelta(minutes=20), last_symbol_watch_alerted_at=None,
            last_key_alert=prior,
        ), "confirmed")
        # A condition that disappeared beyond the confirmation window is a
        # new episode even if the prior alert was in the same session.
        self.assertEqual(intraday_signal_event_state(
            {"signal_type": "entry", "hard": False, "score": 70, "conditions": {}},
            observed_at=now, latest_event_at=now - timedelta(minutes=6),
            last_key_alerted_at=now - timedelta(minutes=20), last_symbol_watch_alerted_at=None,
            last_key_alert=prior,
        ), "confirming")

    def test_live_policy_gate_blocks_new_entry_during_broad_risk_off(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "entry"}, {"available_quantity": 0}, {"price": 10, "price_source": "tencent_batched_watch_quote"},
            {"status": "completed", "trade_constraints": {}},
            {"status": "available", "market_state": "broad_risk_off", "board_snapshot_age_seconds": 30},
            {"status": "confirmed"},
        )
        self.assertFalse(result["allow_confirmation"])
        self.assertIn("broad_risk_off_blocks_new_entry", result["reason_codes"])

    def test_live_policy_gate_blocks_entry_when_daily_or_board_context_is_not_usable(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "entry"}, {"available_quantity": 0}, {"price": 10, "price_source": "tencent_batched_watch_quote"},
            {"status": "data_quality_blocked", "trade_constraints": {}},
            {"status": "missing", "market_state": "unknown"},
            {"status": "missing"},
        )
        self.assertFalse(result["allow_confirmation"])
        self.assertIn("daily_factor_quality_blocked", result["reason_codes"])
        self.assertIn("market_context_missing", result["reason_codes"])

    def test_live_policy_gate_keeps_unsellable_hard_stop_as_risk_alert(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "exit"}, {"entry_price": 10, "available_quantity": 0}, {"price": 9, "price_source": "tencent_batched_watch_quote"},
            {"trade_constraints": {"limit_down": 8}}, {"market_state": "mixed_or_neutral"}, {"status": "confirmed"},
        )
        self.assertEqual(result["decision"], "risk_alert_only")
        self.assertTrue(result["allow_confirmation"])

    def test_live_policy_uses_shared_board_and_st_limit_fallbacks(self):
        from app.live_policy import live_policy_gate
        context = {"status": "available", "market_state": "mixed_or_neutral", "board_snapshot_age_seconds": 30}
        fresh_quote = {"price": 11, "pct_change": 10, "price_source": "tencent_batched_watch_quote",
                       "price_freshness": {"status": "fresh"}}
        growth_board = live_policy_gate(
            {"signal_type": "entry"}, {"symbol": "300001.SZ", "available_quantity": 0}, fresh_quote,
            {"status": "completed", "trade_constraints": {"is_st": False}}, context, {"status": "confirmed"},
        )
        self.assertTrue(growth_board["allow_confirmation"])
        self.assertFalse(growth_board["price_limit_state"]["at_limit_up"])

        growth_limit = live_policy_gate(
            {"signal_type": "entry"}, {"symbol": "300001.SZ", "available_quantity": 0},
            {**fresh_quote, "pct_change": 20},
            {"status": "completed", "trade_constraints": {"is_st": False}}, context, {"status": "confirmed"},
        )
        self.assertFalse(growth_limit["allow_confirmation"])
        self.assertIn("policy_limit_up", growth_limit["risk_flags"])

        st_limit = live_policy_gate(
            {"signal_type": "entry"}, {"symbol": "600001.SH", "available_quantity": 0},
            {**fresh_quote, "pct_change": 5},
            {"status": "completed", "trade_constraints": {"is_st": True}}, context, {"status": "confirmed"},
        )
        self.assertFalse(st_limit["allow_confirmation"])
        self.assertEqual(st_limit["price_limit_state"]["limit_ratio"], 0.05)

    def test_live_policy_keeps_realtime_exact_limit_when_daily_constraint_is_missing(self):
        from app.live_policy import live_policy_gate
        result = live_policy_gate(
            {"signal_type": "entry"}, {"symbol": "600001.SH", "available_quantity": 0},
            {"price": 10.0, "limit_up": 10.0, "pct_change": 3.0,
             "price_source": "tencent_batched_watch_quote", "price_freshness": {"status": "fresh"}},
            {"status": "completed", "trade_constraints": {"is_st": False, "limit_up": None, "limit_down": None}},
            {"status": "available", "market_state": "mixed_or_neutral", "board_snapshot_age_seconds": 30},
            {"status": "confirmed"},
        )
        self.assertFalse(result["allow_confirmation"])
        self.assertEqual(result["price_limit_state"]["limit_up"], 10.0)
