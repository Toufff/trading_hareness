from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.research_prices import CARRIED_FORWARD_FLAG
from app.watchlist_main_wave import (
    FEATURE_KEYS,
    HORIZON_DAYS,
    LOOKBACK_DAYS,
    build_examples,
    chronological_splits,
    fit_logistic,
    main_wave_shadow_signal,
    normalize_bars,
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

    def test_next_session_limit_up_or_suspension_is_not_a_fillable_entry(self) -> None:
        start = date(2026, 1, 1)
        rows = []
        limit_up_index = LOOKBACK_DAYS + 5
        suspended_index = LOOKBACK_DAYS + 10
        for index in range(LOOKBACK_DAYS + 20):
            trading_date = start + timedelta(days=index)
            price = 10.0 + index * 0.05
            open_price = price
            if index == limit_up_index + 1:
                open_price = price * 1.10
            rows.append({
                "symbol": "000001.SZ", "name": "样本", "trading_date": trading_date,
                "open": open_price, "high": price * 1.02, "low": price * 0.98, "close": price,
                "volume": 1000 + index, "amount": price * (1000 + index), "adj_factor": 1.0,
                "is_suspended": index == suspended_index + 1,
                "limit_up": price * 1.10, "limit_down": price * 0.90,
            })
        grouped = normalize_bars(rows)
        examples, _ = build_examples(grouped)
        signal_dates = {item["signal_date"] for item in examples}
        self.assertNotIn(start + timedelta(days=limit_up_index), signal_dates)
        self.assertNotIn(start + timedelta(days=suspended_index), signal_dates)


def _watch_rows(factors, *, closes=None, pre_closes=None, symbol="000001.SZ", start=date(2026, 1, 1)):
    """A continuous single-symbol window; every pre_close is the previous close."""
    closes = list(closes if closes is not None else [10.0 + index * 0.05 for index in range(len(factors))])
    if pre_closes is None:
        pre_closes = [closes[0]] + closes[:-1]
    return [
        {"symbol": symbol, "name": "样本", "trading_date": start + timedelta(days=index),
         "open": closes[index], "high": closes[index] * 1.02, "low": closes[index] * 0.98,
         "close": closes[index], "pre_close": pre_closes[index], "adj_factor": factors[index],
         "volume": 1000 + index, "amount": closes[index] * (1000 + index),
         "is_suspended": False, "limit_up": closes[index] * 1.10, "limit_down": closes[index] * 0.90}
        for index in range(len(factors))
    ]


class NormalizeBarsAdjustmentTests(unittest.TestCase):
    """The factor lane is a session or two behind on every longhu evening.

    Dropping each NULL-factor bar meant the newest sessions -- the only ones a
    live score is built from -- vanished for the entire watchlist, and an
    interior NULL left the surviving rows spliced into a series that only
    looks consecutive.  Both are now decided by the one shared rule.
    """

    def test_a_trailing_factor_gap_keeps_its_bars_and_marks_them(self) -> None:
        rows = _watch_rows([1.3] * 8 + [None, None])
        grouped = normalize_bars(rows)
        bars = grouped["000001.SZ"]
        self.assertEqual(len(bars), 10)
        self.assertEqual([bar["adj_factor"] for bar in bars], [1.3] * 10)
        self.assertEqual([bar["adj_factor_carried"] for bar in bars], [False] * 8 + [True, True])
        self.assertAlmostEqual(bars[-1]["adjusted_close"], bars[-1]["raw_close"] * 1.3)

    def test_the_carried_flag_reaches_the_current_rows_payload(self) -> None:
        rows = _watch_rows([1.3] * (LOOKBACK_DAYS + 1) + [None])
        _, current = build_examples(normalize_bars(rows))
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["quality_flags"], [CARRIED_FORWARD_FLAG])

    def test_a_fetched_current_row_carries_no_flag(self) -> None:
        rows = _watch_rows([1.3] * (LOOKBACK_DAYS + 2))
        _, current = build_examples(normalize_bars(rows))
        self.assertEqual(current[0]["quality_flags"], [])

    def test_an_interior_hole_drops_the_symbol_instead_of_splicing_it(self) -> None:
        rows = _watch_rows([1.3, 1.3, None, 1.3, 1.3])
        self.assertEqual(normalize_bars(rows), {})

    def test_an_ex_rights_signature_in_the_gap_drops_the_symbol(self) -> None:
        rows = _watch_rows([1.4, 1.4, None, None], closes=[10.0, 10.2, 9.2, 9.3],
                           pre_closes=[10.0, 10.0, 9.2, 9.2])
        self.assertEqual(normalize_bars(rows), {})

    def test_one_symbols_refusal_does_not_remove_a_healthy_one(self) -> None:
        rows = (_watch_rows([1.3, 1.3, None, 1.3], symbol="000001.SZ")
                + _watch_rows([1.1, 1.1, 1.1, None], symbol="600000.SH"))
        grouped = normalize_bars(rows)
        self.assertEqual(sorted(grouped), ["600000.SH"])
        self.assertTrue(grouped["600000.SH"][-1]["adj_factor_carried"])

    def test_a_window_with_no_factor_at_all_is_refused(self) -> None:
        self.assertEqual(normalize_bars(_watch_rows([None, None, None])), {})


if __name__ == "__main__":
    unittest.main()
