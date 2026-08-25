import asyncio
import unittest
from datetime import datetime, timezone

from app.intraday_watch_quote_capture import WatchQuoteCaptureDependencies, capture_watch_quotes
from app.runtime_executors import ExecutorSaturatedError


class WatchQuoteCaptureTests(unittest.TestCase):
    @staticmethod
    def dependencies(*, all_a_snapshot, tencent_watch_quotes, sina_quotes, eastmoney_watch_flows, calls):
        def quote_from_all_a(row):
            return dict(row)

        def merge_eastmoney_flows(quotes, rows):
            for row in rows:
                quotes[row["symbol"]] = {"symbol": row["symbol"], "flow_source": "eastmoney"}

        def annotate_percentiles(quotes):
            calls.append("percentiles")
            for quote in quotes.values():
                quote["percentile_annotated"] = True

        def annotate_provenance(quotes, status):
            calls.append(("provenance", status.get("source")))

        def merge_watch_prices(quotes, rows):
            calls.append("watch_prices")
            for row in rows:
                quotes.setdefault(row["symbol"], {"symbol": row["symbol"]})["price_source"] = "tencent_watch_batch"

        def merge_sina_prices(quotes, rows):
            calls.append("sina_prices")
            for row in rows:
                quotes.setdefault(row["symbol"], {"symbol": row["symbol"]})["price_source"] = "sina_watch_batch"

        return WatchQuoteCaptureDependencies(
            now=lambda: 10.0, all_a_snapshot=all_a_snapshot, tencent_watch_quotes=tencent_watch_quotes,
            sina_quotes=sina_quotes, eastmoney_watch_flows=eastmoney_watch_flows,
            quote_from_all_a=quote_from_all_a, merge_eastmoney_flows=merge_eastmoney_flows,
            annotate_percentiles=annotate_percentiles, annotate_flow_provenance=annotate_provenance,
            merge_watch_prices=merge_watch_prices, merge_sina_prices=merge_sina_prices,
            quote_freshness=lambda *_: {"status": "fresh"},
            consume_background_exception=lambda task: task.exception() if not task.cancelled() else None,
            safe_error=lambda detail, _: detail, executor_saturated_error=ExecutorSaturatedError,
            watch_quote_errors=(ValueError,), all_a_snapshot_errors=(ValueError,),
        )

    def test_keeps_direct_watch_prices_and_cross_sectional_percentiles(self):
        async def all_a_snapshot():
            return [{"symbol": "000001.SZ", "close": 10.0}], {"status": "fresh", "cross_sectional": True}

        async def direct(symbols, **_):
            self.assertEqual(symbols, ["000001.SZ"])
            return [{"symbol": "000001.SZ", "close": 10.1}]

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("fallback must not be called when direct watch prices exist")

        calls = []
        capture = asyncio.run(capture_watch_quotes(
            ["000001.SZ"], datetime(2026, 8, 22, 1, tzinfo=timezone.utc), 20.0,
            self.dependencies(
                all_a_snapshot=all_a_snapshot, tencent_watch_quotes=direct,
                sina_quotes=unexpected, eastmoney_watch_flows=unexpected, calls=calls,
            ),
        ))
        self.assertEqual(capture.fresh_watch_rows[0]["close"], 10.1)
        self.assertEqual(capture.quotes["000001.SZ"]["price_source"], "tencent_watch_batch")
        self.assertEqual(capture.quotes["000001.SZ"]["price_freshness"]["status"], "fresh")
        self.assertIn("percentiles", calls)
        self.assertEqual(capture.all_a_snapshot_status["status"], "fresh")

    def test_uses_sina_price_and_eastmoney_flow_only_when_all_a_is_unavailable(self):
        async def all_a_snapshot():
            raise ExecutorSaturatedError("public executor full")

        async def direct(*_args, **_kwargs):
            return []

        async def sina(symbols):
            self.assertEqual(symbols, ["000002.SZ"])
            return [{"symbol": "000002.SZ", "close": 9.9}]

        async def eastmoney(symbols, **_):
            self.assertEqual(symbols, ["000002.SZ"])
            return [{"symbol": "000002.SZ", "main_net_inflow": 12.0}]

        calls = []
        capture = asyncio.run(capture_watch_quotes(
            ["000002.SZ"], datetime(2026, 8, 22, 1, tzinfo=timezone.utc), 45.0,
            self.dependencies(
                all_a_snapshot=all_a_snapshot, tencent_watch_quotes=direct,
                sina_quotes=sina, eastmoney_watch_flows=eastmoney, calls=calls,
            ),
        ))
        self.assertEqual(capture.all_a_snapshot_status["source"], "eastmoney_watch_flow_batch")
        self.assertFalse(capture.all_a_snapshot_status["cross_sectional"])
        self.assertEqual(capture.quotes["000002.SZ"]["price_source"], "sina_watch_batch")
        self.assertEqual(capture.quotes["000002.SZ"]["flow_source"], "eastmoney")
        self.assertNotIn("percentiles", calls)


if __name__ == "__main__":
    unittest.main()
