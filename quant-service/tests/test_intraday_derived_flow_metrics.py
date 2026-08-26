"""Unit coverage for the THS-derived replacement of Eastmoney watch flow.

The derivation exists because the public Eastmoney endpoint fails roughly half
of all live 30-second scans.  These tests pin the two definitions, the refusal
to guess, and the fact that ``main_net_inflow`` is never derived.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.intraday_derived_flow_metrics import (
    SESSION_MINUTES,
    apply_derived_watch_flow_metrics,
    derive_watch_flow_metrics,
    derived_flow_divergence,
    session_elapsed_minutes,
)


def _number(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _at(hour: int, minute: int) -> datetime:
    """One UTC instant expressed from its Shanghai wall-clock time."""
    return datetime(2026, 8, 26, hour - 8, minute, tzinfo=timezone.utc)


class SessionElapsedMinuteTests(unittest.TestCase):
    def test_call_auction_contributes_volume_but_no_continuous_auction_time(self):
        self.assertEqual(session_elapsed_minutes(_at(9, 26)), 0)

    def test_morning_afternoon_and_lunch_break(self):
        self.assertEqual(session_elapsed_minutes(_at(11, 8)), 98)
        self.assertEqual(session_elapsed_minutes(_at(11, 30)), 120)
        self.assertEqual(session_elapsed_minutes(_at(12, 15)), 120, "the lunch break adds no minutes")
        self.assertEqual(session_elapsed_minutes(_at(13, 1)), 121)

    def test_close_and_after_hours_saturate_at_one_session(self):
        self.assertEqual(session_elapsed_minutes(_at(15, 0)), SESSION_MINUTES)
        self.assertEqual(session_elapsed_minutes(_at(21, 30)), SESSION_MINUTES)


class DeriveWatchFlowMetricTests(unittest.TestCase):
    reference = {"000001.SZ": {"float_shares": 100_000_000.0, "mean_daily_volume_shares": 4_800_000.0}}

    def _derive(self, quotes, reference=None, observed_at=None):
        return derive_watch_flow_metrics(
            quotes, reference if reference is not None else self.reference,
            observed_at=observed_at or _at(11, 8), number=_number,
        )

    def test_turnover_rate_is_volume_over_float_shares_as_a_percentage(self):
        derived = self._derive({"000001.SZ": {"volume": 2_000_000.0}})
        self.assertAlmostEqual(derived["000001.SZ"]["turnover_rate"], 2.0, places=5)

    def test_volume_ratio_compares_per_minute_pace_against_the_trailing_mean(self):
        derived = self._derive({"000001.SZ": {"volume": 2_000_000.0}})
        # 2,000,000 shares over 98 minutes against a 4,800,000/240 baseline.
        self.assertAlmostEqual(
            derived["000001.SZ"]["volume_ratio"], (2_000_000 / 98) / (4_800_000 / 240), places=5,
        )

    def test_a_full_session_of_exactly_the_trailing_mean_is_a_volume_ratio_of_one(self):
        derived = self._derive({"000001.SZ": {"volume": 4_800_000.0}}, observed_at=_at(15, 0))
        self.assertAlmostEqual(derived["000001.SZ"]["volume_ratio"], 1.0, places=5)

    def test_missing_reference_or_volume_is_skipped_rather_than_guessed(self):
        self.assertEqual(self._derive({"600000.SH": {"volume": 1_000.0}}), {})
        self.assertEqual(self._derive({"000001.SZ": {"volume": None}}), {})
        self.assertEqual(self._derive({"000001.SZ": {"volume": 0}}), {})

    def test_a_partial_reference_derives_only_the_field_it_supports(self):
        derived = self._derive(
            {"000001.SZ": {"volume": 2_000_000.0}},
            reference={"000001.SZ": {"float_shares": 100_000_000.0, "mean_daily_volume_shares": None}},
        )
        self.assertEqual(set(derived["000001.SZ"]), {"turnover_rate"})

    def test_the_opening_minutes_produce_no_volume_ratio(self):
        derived = self._derive({"000001.SZ": {"volume": 2_000_000.0}}, observed_at=_at(9, 31))
        self.assertEqual(set(derived["000001.SZ"]), {"turnover_rate"},
                         "a one-minute denominator is opening noise, not a ratio")

    def test_main_net_inflow_is_never_derived(self):
        derived = self._derive({"000001.SZ": {"volume": 2_000_000.0, "main_net_inflow": None}})
        self.assertNotIn("main_net_inflow", derived["000001.SZ"])


class ApplyAndDivergenceTests(unittest.TestCase):
    def test_derived_values_replace_eastmoney_and_retain_the_observed_value(self):
        quotes = {"000001.SZ": {"volume_ratio": 1.9, "turnover_rate": 2.05, "main_net_inflow": 12.0}}
        sources = apply_derived_watch_flow_metrics(
            quotes, {"000001.SZ": {"volume_ratio": 1.92, "turnover_rate": 2.0}},
        )
        self.assertEqual(quotes["000001.SZ"]["volume_ratio"], 1.92)
        self.assertEqual(quotes["000001.SZ"]["volume_ratio_eastmoney_observed"], 1.9)
        self.assertEqual(sources["000001.SZ"], {
            "volume_ratio": "fuyao_ths_derived", "turnover_rate": "fuyao_ths_derived",
            "main_net_inflow": "eastmoney_watch_flow",
        })

    def test_fields_that_cannot_be_derived_keep_the_eastmoney_fallback(self):
        quotes = {"000001.SZ": {"volume_ratio": 1.9, "turnover_rate": 2.05, "main_net_inflow": 12.0}}
        apply_derived_watch_flow_metrics(quotes, {"000001.SZ": {"turnover_rate": 2.0}})
        self.assertEqual(quotes["000001.SZ"]["volume_ratio"], 1.9)
        self.assertEqual(quotes["000001.SZ"]["flow_metric_sources"]["volume_ratio"], "eastmoney_watch_flow")

    def test_an_eastmoney_outage_leaves_derived_fields_standing_and_the_rest_unavailable(self):
        quotes = {"000001.SZ": {}}
        apply_derived_watch_flow_metrics(quotes, {"000001.SZ": {"volume_ratio": 1.92, "turnover_rate": 2.0}})
        self.assertEqual(quotes["000001.SZ"]["flow_metric_sources"], {
            "volume_ratio": "fuyao_ths_derived", "turnover_rate": "fuyao_ths_derived",
            "main_net_inflow": "unavailable",
        })

    def test_divergence_reports_only_symbols_where_both_sources_answered(self):
        quotes = {"000001.SZ": {"volume_ratio": 2.0, "turnover_rate": 2.0}, "000002.SZ": {}}
        derived = {"000001.SZ": {"volume_ratio": 2.02, "turnover_rate": 2.0},
                   "000002.SZ": {"volume_ratio": 3.0, "turnover_rate": 3.0}}
        apply_derived_watch_flow_metrics(quotes, derived)
        summary = derived_flow_divergence(quotes, derived, number=_number)
        self.assertEqual(summary["volume_ratio"]["compared_symbols"], 1)
        self.assertAlmostEqual(summary["volume_ratio"]["median_abs_error_pct"], 1.0, places=4)
        self.assertEqual(summary["turnover_rate"]["median_abs_error_pct"], 0.0)

    def test_a_full_eastmoney_outage_reports_no_comparison_rather_than_a_perfect_score(self):
        quotes = {"000001.SZ": {}}
        derived = {"000001.SZ": {"volume_ratio": 2.0, "turnover_rate": 2.0}}
        apply_derived_watch_flow_metrics(quotes, derived)
        summary = derived_flow_divergence(quotes, derived, number=_number)
        self.assertEqual(summary["volume_ratio"]["compared_symbols"], 0)
        self.assertIsNone(summary["volume_ratio"]["median_abs_error_pct"])


if __name__ == "__main__":
    unittest.main()
