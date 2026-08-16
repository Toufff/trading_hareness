from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.watchlist_countertrend_rebound import (
    build_rebound_examples,
    countertrend_rebound_failure_reduce_signal,
    countertrend_rebound_realtime_signal,
    evaluate_rebound_split,
    rebound_state,
)
from app.watchlist_main_wave import FEATURE_KEYS


def features(**overrides: float) -> dict[str, float]:
    values = {key: 0.0 for key in FEATURE_KEYS}
    values.update({
        "return_1d": 0.04, "return_3d": 0.08, "return_5d": 0.10,
        "return_20d": -0.20, "ma5_gap": 0.03, "prior_high_20_gap": -0.25,
        "close_location": 0.80, "volume_ratio_20d": 1.3,
    })
    values.update(overrides)
    return values


def context(**overrides: float) -> dict[str, float]:
    values = {
        "above_ma5_ratio": 0.75, "median_return_3d": 0.05,
        "median_return_20d": -0.18, "previous_market_breadth": 0.65,
    }
    values.update(overrides)
    return values


class CountertrendReboundTests(unittest.TestCase):
    def test_panic_probe_and_confirmation_are_distinct(self) -> None:
        panic = rebound_state(
            features(return_1d=0.0, return_3d=-0.10, ma5_gap=-0.08),
            {"breadth": 0.10, "median_change_pct": -3.2},
            context(above_ma5_ratio=0.15, median_return_3d=-0.08, previous_market_breadth=0.70),
        )
        self.assertEqual(panic["state"], "panic")
        probe = rebound_state(
            features(ma5_gap=-0.01), {"breadth": 0.90, "median_change_pct": 2.5},
            context(above_ma5_ratio=0.65, median_return_3d=0.03, previous_market_breadth=0.10),
        )
        self.assertEqual(probe["state"], "probe")
        confirmed = rebound_state(
            features(), {"breadth": 0.65, "median_change_pct": 1.0}, context(),
        )
        self.assertEqual(confirmed["state"], "confirmed")

    def test_only_confirmed_rows_enter_candidate_evaluation(self) -> None:
        rows = []
        for index, state in enumerate(("panic", "probe", "confirmed")):
            rows.append({
                "symbol": f"00000{index}.SZ", "signal_date": date(2026, 8, 4),
                "label": 1, "model_score": 0.8, "pattern": {"state": state},
                "terminal_return": 0.10, "maximum_favorable_excursion": 0.15,
                "maximum_adverse_excursion": -0.02,
            })
        metrics, selected = evaluate_rebound_split(rows)
        self.assertEqual([item["pattern"]["state"] for item in selected], ["confirmed"])
        self.assertEqual(metrics["panic_rows"], 1)

    def test_live_shadow_requires_confirmed_daily_state(self) -> None:
        watch = {"symbol": "000636.SZ", "alert_on_entry": True, "entry_price": None}
        quote = {"price": 58.0, "pct_change": 2.0, "volume_ratio": 1.8, "turnover_rate": 3.2,
                 "main_net_inflow": 1_000_000}
        minute = {"return_3m_pct": 0.8, "minute_volume_multiple": 2.0, "above_vwap_pct": 0.3}
        self.assertIsNone(countertrend_rebound_realtime_signal(
            watch, quote, minute, {"confirming_peer_count": 0},
            {"state": "shadow_panic", "model_score": 0.8},
        ))
        signal = countertrend_rebound_realtime_signal(
            watch, quote, minute, {"confirming_peer_count": 0},
            {"state": "shadow_confirmed", "model_score": 0.8,
             "research_probability": {"estimated_probability": 0.31}},
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal_type"], "entry")
        self.assertNotIn("shadow_only", signal)
        self.assertEqual(signal["conditions"]["research_probability"]["estimated_probability"], 0.31)
        self.assertIn("panic_stage_is_not_entry", signal["risk_flags"])

    def test_live_rebound_respects_explicit_watch_alert_and_existing_position(self) -> None:
        quote = {"price": 10, "pct_change": 2.0, "volume_ratio": 1.8, "main_net_inflow": 1}
        minute = {"return_3m_pct": 0.8, "minute_volume_multiple": 2.0, "above_vwap_pct": 0.3}
        prior = {"state": "shadow_confirmed", "model_score": 0.8}
        self.assertIsNone(countertrend_rebound_realtime_signal(
            {"symbol": "000001.SZ", "alert_on_entry": False}, quote, minute, {}, prior,
        ))
        self.assertIsNone(countertrend_rebound_realtime_signal(
            {"symbol": "000001.SZ", "alert_on_entry": True, "entry_price": 9.5}, quote, minute, {}, prior,
        ))

    def test_rebound_failure_requires_vwap_momentum_and_flow_or_peer_loss(self) -> None:
        watch = {"symbol": "000001.SZ", "entry_price": 10.0, "alert_on_exit": True}
        quote = {"price": 9.85, "pct_change": -1.5, "volume_ratio": 1.8, "main_net_inflow": -100}
        failure = countertrend_rebound_failure_reduce_signal(
            watch, quote, {"return_3m_pct": -0.8, "above_vwap_pct": -0.3},
            {"available_peer_count": 3, "confirming_peer_count": 0},
            {"state": "shadow_confirmed", "model_score": 0.7},
        )
        self.assertIsNotNone(failure)
        self.assertEqual(failure["signal_type"], "reduce")
        self.assertTrue(failure["conditions"]["vwap_acceptance_lost"])
        self.assertTrue(failure["conditions"]["peer_confirmation_lost"])
        self.assertIsNone(countertrend_rebound_failure_reduce_signal(
            watch, {**quote, "main_net_inflow": 100},
            {"return_3m_pct": -0.2, "above_vwap_pct": -0.1},
            {"available_peer_count": 3, "confirming_peer_count": 2}, {},
        ))
        self.assertIsNone(countertrend_rebound_failure_reduce_signal(
            {**watch, "alert_on_exit": False}, quote,
            {"return_3m_pct": -0.8, "above_vwap_pct": -0.3}, {},
            {"state": "shadow_confirmed", "model_score": 0.7},
        ))

    def test_rebound_failure_does_not_mislabel_an_unrelated_position(self) -> None:
        watch = {"symbol": "000001.SZ", "entry_price": 10.0, "alert_on_exit": True}
        quote = {"price": 9.70, "main_net_inflow": -100}
        self.assertIsNone(countertrend_rebound_failure_reduce_signal(
            watch, quote, {"return_3m_pct": -0.9, "above_vwap_pct": -0.4},
            {"available_peer_count": 3, "confirming_peer_count": 0},
            {"state": "shadow_decline", "model_score": 0.7},
        ))

    def test_next_session_suspension_is_not_treated_as_fillable_entry(self) -> None:
        start = date(2026, 1, 1)
        bars = []
        market = []
        for index in range(70):
            trading_date = start + timedelta(days=index)
            price = 10.0 + index * 0.05
            bars.append({
                "symbol": "000001.SZ", "name": "样本", "trading_date": trading_date,
                "open": price, "high": price * 1.02, "low": price * 0.98, "close": price,
                "volume": 1000 + index, "amount": price * (1000 + index), "adj_factor": 1.0,
                "is_suspended": index == 61, "limit_up": price * 1.1, "limit_down": price * 0.9,
            })
            market.append({
                "trading_date": trading_date, "stock_count": 100,
                "advancers": 60, "decliners": 40, "median_change_pct": 0.5,
            })
        examples, _, _ = build_rebound_examples(bars, market)
        self.assertNotIn(start + timedelta(days=60), {item["signal_date"] for item in examples})


if __name__ == "__main__":
    unittest.main()
