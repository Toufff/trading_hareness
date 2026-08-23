import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.free_market_providers import _tencent_order_book_row
from app.intraday_attribution import signal_attribution
from app.intraday_features import minute_features
from app.intraday_outcomes import a_share_return_decomposition
from app.numeric_utils import intraday_number
from app.order_book_features import aggregate_order_book_observations, order_book_observation


class IntradayMicrostructureTests(unittest.TestCase):
    def test_order_book_observation_uses_depth_and_nonnegative_cumulative_deltas(self):
        previous = {"bids": [{"price": 10.00, "size": 100}, {"price": 9.99, "size": 50}], "asks": [{"price": 10.01, "size": 120}, {"price": 10.02, "size": 60}], "cumulative_volume_lot": 1000, "cumulative_amount": 1_000_000, "outer_volume_lot": 600, "inner_volume_lot": 400}
        current = {"bids": [{"price": 10.01, "size": 180}, {"price": 10.00, "size": 80}], "asks": [{"price": 10.02, "size": 90}, {"price": 10.03, "size": 50}], "cumulative_volume_lot": 1010, "cumulative_amount": 1_010_500, "outer_volume_lot": 620, "inner_volume_lot": 405}
        observed = order_book_observation(current, previous)
        self.assertEqual(observed["status"], "observed")
        self.assertEqual(observed["delta_status"], "ready")
        self.assertGreater(observed["qi1"], 0)
        self.assertGreater(observed["ofi_best_level"], 0)
        self.assertEqual(observed["cumulative_volume_delta_lot"], 10.0)
        self.assertEqual(observed["interval_vwap"], 10.5)

    def test_tencent_order_book_decoder_preserves_a_limit_up_seal_with_empty_asks(self):
        values = [""] * 36
        values[1], values[3], values[4] = "涨停样本", "10.99", "9.99"
        values[6], values[7], values[8] = "1000", "610", "390"
        values[9], values[10] = "10.99", "557769"
        for level in range(1, 5): values[9 + level * 2], values[10 + level * 2] = "0.00", "0"
        for level in range(5): values[19 + level * 2], values[20 + level * 2] = "0.00", "0"
        values[30], values[35] = "20260812130000", "10.99/1000/1099000"
        row = _tencent_order_book_row("000001.SZ", values)
        self.assertIsNotNone(row)
        self.assertTrue(row["one_sided_book"])
        self.assertEqual(row["book_side"], "bid_only")
        self.assertEqual(row["seal_volume_lot"], 557769.0)
        observed = order_book_observation(row)
        self.assertEqual(observed["qi1"], 1.0)
        self.assertEqual(observed["qi5"], 1.0)

    def test_counter_reset_and_zero_amount_do_not_invent_vwap(self):
        reset = order_book_observation({"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}], "cumulative_volume_lot": 10, "cumulative_amount": 1000, "outer_volume_lot": 4, "inner_volume_lot": 6}, {"bids": [{"price": 10, "size": 9}], "asks": [{"price": 10.01, "size": 11}], "cumulative_volume_lot": 100, "cumulative_amount": 10_000, "outer_volume_lot": 50, "inner_volume_lot": 50})
        self.assertEqual(reset["cumulative_volume_delta_lot"], 0.0)
        self.assertIsNone(reset["interval_vwap"])
        unchanged_amount = order_book_observation({"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}], "cumulative_volume_lot": 11, "cumulative_amount": 1000}, {"bids": [{"price": 10, "size": 10}], "asks": [{"price": 10.01, "size": 10}], "cumulative_volume_lot": 10, "cumulative_amount": 1000})
        self.assertEqual(unchanged_amount["cumulative_amount_delta"], 0.0)
        self.assertIsNone(unchanged_amount["interval_vwap"])

    def test_levels_keep_depth_positions_for_qi_weights(self):
        observed = order_book_observation({"bids": [{"price": 10, "size": 100}, {"price": 0, "size": 0}, {"price": 9.98, "size": 100}], "asks": [{"price": 10.01, "size": 100}, {"price": 10.02, "size": 100}, {"price": 10.03, "size": 100}]})
        self.assertAlmostEqual(observed["bid_depth_lot"], 136.79, places=2)

    def test_order_flow_aggregates_windows_and_proxies(self):
        at = datetime(2026, 8, 12, 5, 5, tzinfo=timezone.utc)
        rows = [{"observed_at": at - timedelta(seconds=offset), "raw": {"order_book_features": {"ofi_best_level": value, "book_mid": 10.0 + value / 100, "qi5": 0.25, "bid_depth_lot": 100, "ask_depth_lot": 100, "outer_inner_delta_lot": value, "seal_volume_delta_lot": -offset}}} for offset, value in ((3, 2), (6, 3), (9, -1), (70, 9), (310, 99))]
        aggregate = aggregate_order_book_observations(rows, at)
        self.assertEqual(aggregate["ofi_30s"], 4.0)
        self.assertEqual(aggregate["ofi_30s_sample_count"], 3)
        self.assertEqual(aggregate["ofi_1m"], 4.0)
        self.assertEqual(aggregate["ofi_5m"], 13.0)
        self.assertIsNotNone(aggregate["kyle_lambda_proxy_5m"])
        self.assertIsNotNone(aggregate["vpin_proxy_5m"])
        self.assertIsNotNone(aggregate["cord_sign_alignment_5m"])

    def test_smart_money_uses_same_window_vwap_and_volume_share(self):
        rows = [{"time": f"10{index:02d}", "close": 10.0 + index * 0.01, "volume_lot": 100 if index < 29 else 1000, "amount": (10.0 + index * 0.01) * (100 if index < 29 else 1000) * 100, "vwap": 9.0} for index in range(30)]
        features = minute_features(rows, number=intraday_number)
        self.assertIsNotNone(features)
        self.assertGreaterEqual(features["smart_money_selected_volume_share_30m"], 0.2)
        self.assertNotEqual(features["smart_money_window_vwap_30m"], 9.0)

    def test_return_decomposition_and_attribution_remain_observational(self):
        result = a_share_return_decomposition(Decimal("10"), 1, Decimal("10.5"), Decimal("10.2"), Decimal("10.8"))
        self.assertEqual(result["trigger_to_close"], Decimal("0.05"))
        self.assertEqual(result["trigger_to_next_close"], Decimal("0.08"))
        attribution = signal_attribution("000001.SZ:watch:test", "watch", {}, {"tencent_order_book": {"status": "observed", "latest_features": {"status": "observed", "delta_status": "ready", "qi5": 0.4}, "ofi_30s": 3, "ofi_30s_sample_count": 3}, "tencent_minute": {"price_log_volume_corr_30m": -0.4, "smart_money_q_30m": 0.99}}, number=intraday_number, signal_model_version="watchlist-confirmation-v5")
        self.assertEqual(attribution["microstructure_state"], "observed_bid_heavy_positive_ofi_30s")
        self.assertEqual(attribution["ofi_attribution_window"], "30s")
        self.assertEqual(attribution["price_volume_state"], "negative_corr")
