from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.watchlist_main_wave import (
    FEATURE_KEYS,
    HORIZON_DAYS,
    chronological_splits,
    fit_logistic,
    main_wave_shadow_signal,
    score_features,
)


class WatchlistMainWaveTests(unittest.TestCase):
    def test_chronological_split_embargo_separates_future_labels(self) -> None:
        start = date(2026, 1, 1)
        examples = [
            {"signal_date": start + timedelta(days=index), "label": index % 2,
             "features": {key: float(index % 7) for key in FEATURE_KEYS}}
            for index in range(120)
        ]
        splits, contract = chronological_splits(examples)
        self.assertEqual(contract["embargo_trading_days"], HORIZON_DAYS)
        self.assertLess(max(row["signal_date"] for row in splits["train"]),
                        min(row["signal_date"] for row in splits["validation"]) - timedelta(days=HORIZON_DAYS - 1))
        self.assertLess(max(row["signal_date"] for row in splits["validation"]),
                        min(row["signal_date"] for row in splits["test"]) - timedelta(days=HORIZON_DAYS - 1))

    def test_logistic_processor_is_fit_only_from_supplied_rows(self) -> None:
        rows = []
        for index in range(80):
            label = int(index >= 40)
            features = {key: 0.0 for key in FEATURE_KEYS}
            features["return_20d"] = -1.0 if label == 0 else 1.0
            rows.append({"label": label, "features": features})
        model = fit_logistic(rows)
        low = {key: 0.0 for key in FEATURE_KEYS}; low["return_20d"] = -1.0
        high = {key: 0.0 for key in FEATURE_KEYS}; high["return_20d"] = 1.0
        self.assertLess(score_features(low, model), score_features(high, model))
        self.assertEqual(model["fit_rows"], 80)

    def test_shadow_signal_requires_daily_prior_and_intraday_confirmation(self) -> None:
        watch = {"symbol": "000636.SZ"}
        prior = {"state": "shadow_top_quintile", "model_score": 0.72, "percentile": 0.9}
        signal = main_wave_shadow_signal(
            watch, {"pct_change": 2.2, "volume_ratio": 1.8, "main_net_inflow": 1_000_000},
            {"return_3m_pct": 0.8, "minute_volume_multiple": 2.1, "above_vwap_pct": 0.4},
            {"confirming_peer_count": 0}, prior,
        )
        self.assertIsNotNone(signal)
        self.assertTrue(signal["shadow_only"])
        self.assertIn("no_feishu_alert", signal["risk_flags"])
        rejected = main_wave_shadow_signal(
            watch, {"pct_change": 2.2, "volume_ratio": 1.8, "main_net_inflow": 1_000_000},
            {"return_3m_pct": -0.2, "minute_volume_multiple": 2.1, "above_vwap_pct": 0.4},
            {"confirming_peer_count": 0}, prior,
        )
        self.assertIsNone(rejected)


if __name__ == "__main__":
    unittest.main()
