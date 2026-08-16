from __future__ import annotations

import unittest
from datetime import date

from app.watchlist_main_wave import FEATURE_KEYS
from app.watchlist_main_wave_v2 import (
    evaluate_pattern_split,
    main_wave_pattern,
    main_wave_v2_shadow_signal,
)


def features(**overrides: float) -> dict[str, float]:
    values = {key: 0.0 for key in FEATURE_KEYS}
    values.update({
        "return_20d": 0.08,
        "ma20_60_gap": 0.04,
        "prior_high_20_gap": -0.02,
        "volume_ratio_20d": 1.5,
        "volume_5_20_ratio": 1.0,
        "close_location": 0.8,
        "range_5d": 0.03,
        "range_20d": 0.04,
    })
    values.update(overrides)
    return values


class WatchlistMainWaveV2Tests(unittest.TestCase):
    def test_pattern_distinguishes_confirmed_forming_and_observe(self) -> None:
        self.assertEqual(main_wave_pattern(features())["state"], "confirmed")
        forming = features(
            prior_high_20_gap=-0.06, volume_ratio_20d=0.75,
            volume_5_20_ratio=0.75, range_5d=0.02, range_20d=0.04,
        )
        self.assertEqual(main_wave_pattern(forming)["state"], "forming")
        self.assertEqual(main_wave_pattern(features(return_20d=-0.15))["state"], "observe")

    def test_evaluation_may_abstain_instead_of_forcing_daily_selection(self) -> None:
        rows = []
        for index in range(8):
            rows.append({
                "symbol": f"00000{index}.SZ", "signal_date": date(2026, 8, 14),
                "label": index % 2, "features": features(return_20d=-0.12),
                "terminal_return": 0.01, "maximum_favorable_excursion": 0.03,
                "maximum_adverse_excursion": -0.02,
            })
        metrics, selected = evaluate_pattern_split(rows)
        self.assertEqual(selected, [])
        self.assertEqual(metrics["selected_rows"], 0)
        self.assertEqual(metrics["abstained_dates"], 1)

    def test_forming_prior_requires_stricter_intraday_confirmation(self) -> None:
        watch = {"symbol": "000636.SZ"}
        prior = {"state": "shadow_forming", "model_score": 0.74}
        weak = main_wave_v2_shadow_signal(
            watch, {"pct_change": 1.4, "volume_ratio": 1.8, "main_net_inflow": 1_000_000},
            {"return_3m_pct": 0.8, "minute_volume_multiple": 1.8, "above_vwap_pct": 0.4},
            {"confirming_peer_count": 0}, prior,
        )
        self.assertIsNone(weak)
        confirmed = main_wave_v2_shadow_signal(
            watch, {"pct_change": 1.8, "volume_ratio": 2.2, "main_net_inflow": 1_000_000},
            {"return_3m_pct": 1.2, "minute_volume_multiple": 2.3, "above_vwap_pct": 0.4},
            {"confirming_peer_count": 0}, prior,
        )
        self.assertIsNotNone(confirmed)
        self.assertTrue(confirmed["shadow_only"])
        self.assertIn("no_feishu_alert", confirmed["risk_flags"])


if __name__ == "__main__":
    unittest.main()
