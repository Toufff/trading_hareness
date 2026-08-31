"""Unit coverage for signal_rules()'s entry_* challenger override parameters.

Every default must reproduce today's live behavior exactly (these overrides
exist only for strategy_timing_challengers.py's offline replay harness).
"""

from __future__ import annotations

from datetime import datetime, timezone

from provider_test_support import *  # noqa: F403


def _fixture():
    watch = {"symbol": "000001.SZ", "entry_price": None, "available_quantity": 0, "alert_on_entry": True, "alert_on_exit": True}
    quote = {"price": 10.2, "pct_change": 2.0, "volume_ratio": 2.0, "turnover_rate": 4.0,
             "main_net_inflow": 100, "main_flow_percentile": 0.95}
    return watch, quote


def _evaluate(watch, quote, previous_quote=None, minute_features=None, **overrides):
    return isolated_signal_rules(
        watch, quote, previous_quote or {"price": 10.1}, None, minute_features, None,
        number=pure_intraday_number,
        upside_assessment_fn=lambda q, d, m, p: isolated_upside_assessment(
            q, d, m, p, number=pure_intraday_number, eac_window=pure_eac_window,
        ),
        model_version="watchlist-confirmation-v6", **overrides,
    )


class EntryTimingOverrideTests(unittest.TestCase):
    def test_defaults_reproduce_live_behavior_exactly(self):
        watch, quote = _fixture()
        without_overrides = _evaluate(watch, quote)
        with_default_overrides = _evaluate(
            watch, quote, entry_min_pct=1.0, entry_max_pct=6.5,
            entry_requires_minute_confirmation=False, entry_session_windows=None,
        )
        self.assertEqual(without_overrides, with_default_overrides)
        self.assertTrue(any(signal["signal_type"] == "entry" for signal in without_overrides),
                        "the fixture must actually fire entry_setup for the rest of this test class to be meaningful")

    def test_c1_tighter_ceiling_rejects_a_trigger_above_it(self):
        watch, quote = _fixture()  # pct_change=2.0
        fired = _evaluate(watch, quote, entry_max_pct=1.5)
        self.assertFalse(any(signal["signal_type"] == "entry" for signal in fired))
        allowed = _evaluate(watch, quote, entry_max_pct=3.0)
        self.assertTrue(any(signal["signal_type"] == "entry" for signal in allowed))

    def test_c2_session_window_gates_on_scan_time(self):
        watch, quote = _fixture()
        windows = (("09:30", "10:00"),)
        inside = {**quote, "_scan_observed_at": datetime(2026, 8, 25, 1, 35, tzinfo=timezone.utc)}  # 09:35 CST
        outside = {**quote, "_scan_observed_at": datetime(2026, 8, 25, 6, 35, tzinfo=timezone.utc)}  # 14:35 CST
        self.assertTrue(any(signal["signal_type"] == "entry" for signal in _evaluate(
            watch, inside, entry_session_windows=windows,
        )))
        self.assertFalse(any(signal["signal_type"] == "entry" for signal in _evaluate(
            watch, outside, entry_session_windows=windows,
        )))

    def test_c3_minute_confirmation_requires_volume_and_vwap(self):
        watch, quote = _fixture()
        unconfirmed = _evaluate(watch, quote, entry_requires_minute_confirmation=True)
        self.assertFalse(any(signal["signal_type"] == "entry" for signal in unconfirmed))
        confirmed = _evaluate(
            watch, quote, minute_features={"minute_volume_multiple": 3.5, "above_vwap_pct": 1.0},
            entry_requires_minute_confirmation=True,
        )
        self.assertTrue(any(signal["signal_type"] == "entry" for signal in confirmed))


if __name__ == "__main__":
    unittest.main()
