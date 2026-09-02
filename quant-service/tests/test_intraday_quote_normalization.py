from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.intraday_quote_normalization import (
    LONGHU_WATCH_QUOTE_VOLUME_UNIT,
    annotate_flow_percentiles,
    exchange_time_status,
    merge_eastmoney_watch_flows,
    merge_longhu_watch_quotes,
    merge_sina_watch_quotes,
    merge_watch_quote_prices,
    observation_source,
    quote_from_fuyao,
)


def number(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class IntradayQuoteNormalizationTests(unittest.TestCase):
    def test_dedicated_watch_price_overlays_cross_section_without_dropping_flow(self) -> None:
        quotes = {"000001.SZ": {"symbol": "000001.SZ", "main_net_inflow": 8.0, "raw": {"all_a": True}}}
        merge_watch_quote_prices(quotes, [{"ts_code": "000001.SZ", "price": "10.2", "pre_close": "10", "trade_time": "20260817093005"}], number=number)
        self.assertEqual(quotes["000001.SZ"]["price_source"], "tencent_batched_watch_quote")
        self.assertEqual(quotes["000001.SZ"]["main_net_inflow"], 8.0)
        self.assertEqual(quotes["000001.SZ"]["pct_change"], 2.0)
        self.assertEqual(observation_source(quotes["000001.SZ"]), "tencent_free")

    def test_sina_and_eastmoney_keep_price_and_flow_semantics_separate(self) -> None:
        quotes: dict[str, dict[str, object]] = {}
        merge_sina_watch_quotes(quotes, [{"ts_code": "000001.SZ", "close": "10.1", "pre_close": "10", "trade_date": "20260817", "trade_time": "093001"}], number=number)
        merge_eastmoney_watch_flows(quotes, [{"ts_code": "000001.SZ", "main_net_inflow": "12", "volume_ratio": "3"}], number=number)
        self.assertEqual(quotes["000001.SZ"]["price_source"], "sina_batched_watch_quote")
        self.assertEqual(quotes["000001.SZ"]["main_net_inflow"], 12.0)
        self.assertIsNone(quotes["000001.SZ"]["main_flow_percentile"])
        self.assertEqual(observation_source(quotes["000001.SZ"]), "sina_free")

    def test_sina_never_overwrites_an_already_priced_quote(self) -> None:
        quotes = {"000001.SZ": {"symbol": "000001.SZ", "price": 10.2, "pct_change": 2.0,
                                 "price_source": "tencent_batched_watch_quote", "raw": {}}}
        merge_sina_watch_quotes(
            quotes, [{"ts_code": "000001.SZ", "close": "99.9", "pre_close": "10", "trade_date": "20260817", "trade_time": "093001"}],
            number=number,
        )
        self.assertEqual(quotes["000001.SZ"]["price"], 10.2)
        self.assertEqual(quotes["000001.SZ"]["price_source"], "tencent_batched_watch_quote")

    def test_timestamp_and_percentile_contracts_are_explicit(self) -> None:
        observed_at = datetime(2026, 8, 17, 1, 30, 10, tzinfo=timezone.utc)
        fresh = exchange_time_status({"price_trade_time": "20260817093005"}, observed_at, 20)
        self.assertEqual(fresh["status"], "fresh")
        quotes = {"a": {"main_net_inflow": -2}, "b": {"main_net_inflow": 6}, "c": {}}
        annotate_flow_percentiles(quotes)
        self.assertEqual(quotes["a"]["main_flow_percentile"], 0.0)
        self.assertEqual(quotes["b"]["main_flow_percentile"], 1.0)

    def test_longhu_volume_is_annotated_with_its_native_unit(self) -> None:
        # Longhu's cumulative volume must not be silently mixed with
        # Tencent's board-lot or Sina's share-based volume under the same
        # ambiguous "volume" key; callers need volume_unit to reconcile them.
        quotes: dict[str, dict[str, object]] = {}
        merge_longhu_watch_quotes(
            quotes,
            [{"ts_code": "000001.SZ", "price": "10.2", "pre_close": "10", "volume": "123456"}],
            number=number,
        )
        self.assertEqual(quotes["000001.SZ"]["volume"], 123456.0)
        self.assertEqual(quotes["000001.SZ"]["volume_unit"], LONGHU_WATCH_QUOTE_VOLUME_UNIT)

    def test_fuyao_mapper_does_not_claim_invalid_codes(self) -> None:
        quote = quote_from_fuyao({"symbol": "000001.SZ", "price": "10", "pct_change": "1.2"})
        self.assertEqual(quote["symbol"] if quote else None, "000001.SZ")
        self.assertIsNone(quote_from_fuyao({"symbol": "bad", "price": "10"}))


if __name__ == "__main__":
    unittest.main()
