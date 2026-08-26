import asyncio
import dataclasses
import unittest
from datetime import datetime, timezone

from app.intraday_derived_flow_metrics import (
    apply_derived_watch_flow_metrics as pure_apply_derived_watch_flow_metrics,
    derive_watch_flow_metrics as pure_derive_watch_flow_metrics,
    derived_flow_divergence as pure_derived_flow_divergence,
)
from app.intraday_watch_quote_capture import WatchQuoteCaptureDependencies, capture_watch_quotes
from app.runtime_executors import ExecutorSaturatedError


class WatchQuoteCaptureTests(unittest.TestCase):
    @staticmethod
    def dependencies(*, all_a_snapshot, tencent_watch_quotes, sina_quotes, eastmoney_watch_flows, calls,
                     watch_flow_reference=None, derive_flow_metrics=None, volume_fallback=None):
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

        async def no_reference(symbols, observed_at):
            calls.append("reference")
            return {}

        async def no_volume_fallback(symbols):
            calls.append("volume_fallback")
            return {}

        return WatchQuoteCaptureDependencies(
            now=lambda: 10.0, all_a_snapshot=all_a_snapshot, tencent_watch_quotes=tencent_watch_quotes,
            sina_quotes=sina_quotes, eastmoney_watch_flows=eastmoney_watch_flows,
            watch_flow_reference=watch_flow_reference or no_reference,
            watch_volume_fallback=volume_fallback or no_volume_fallback,
            derive_flow_metrics=derive_flow_metrics or (lambda quotes, reference, *, observed_at: {}),
            apply_derived_flow_metrics=pure_apply_derived_watch_flow_metrics,
            derived_flow_divergence=lambda quotes, derived: pure_derived_flow_divergence(
                quotes, derived, number=lambda value: float(value) if value is not None else None,
            ),
            quote_from_all_a=quote_from_all_a, merge_eastmoney_flows=merge_eastmoney_flows,
            annotate_percentiles=annotate_percentiles, annotate_flow_provenance=annotate_provenance,
            merge_watch_prices=merge_watch_prices, merge_sina_prices=merge_sina_prices,
            quote_freshness=lambda *_: {"status": "fresh"},
            consume_background_exception=lambda task: task.exception() if not task.cancelled() else None,
            safe_error=lambda detail, _: detail, executor_saturated_error=ExecutorSaturatedError,
            watch_quote_errors=(ValueError,), watch_flow_reference_errors=(ValueError,),
            all_a_snapshot_errors=(ValueError,),
        )

    def test_keeps_direct_watch_prices_and_cross_sectional_percentiles(self):
        async def all_a_snapshot():
            return [{"symbol": "000001.SZ", "close": 10.0}], {"status": "fresh", "cross_sectional": True}

        async def direct(symbols, **_):
            self.assertEqual(symbols, ["000001.SZ"])
            return [{"symbol": "000001.SZ", "close": 10.1}]

        async def eastmoney(symbols, **_kwargs):
            self.assertEqual(symbols, ["000001.SZ"])
            return [{"symbol": "000001.SZ", "main_net_inflow": 12.0}]

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("Sina must not be called when direct watch prices exist")

        calls = []
        capture = asyncio.run(capture_watch_quotes(
            ["000001.SZ"], datetime(2026, 8, 22, 1, tzinfo=timezone.utc), 20.0,
            self.dependencies(
                all_a_snapshot=all_a_snapshot, tencent_watch_quotes=direct,
                sina_quotes=unexpected, eastmoney_watch_flows=eastmoney, calls=calls,
            ),
        ))
        self.assertEqual(capture.fresh_watch_rows[0]["close"], 10.1)
        self.assertEqual(capture.quotes["000001.SZ"]["price_source"], "tencent_watch_batch")
        self.assertEqual(capture.quotes["000001.SZ"]["price_freshness"]["status"], "fresh")
        self.assertIn("percentiles", calls)
        self.assertEqual(capture.all_a_snapshot_status["status"], "fresh")
        self.assertEqual(capture.eastmoney_watch_flow_status["status"], "fresh")
        self.assertTrue(capture.eastmoney_watch_flow_status["research_confirmation_only"])

    def test_keeps_all_a_failure_separate_from_parallel_eastmoney_watch_flow(self):
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
        self.assertEqual(capture.all_a_snapshot_status["status"], "unavailable")
        self.assertEqual(capture.eastmoney_watch_flow_status["source"], "eastmoney_watch_flow_batch")
        self.assertFalse(capture.eastmoney_watch_flow_status["cross_sectional"])
        self.assertEqual(capture.quotes["000002.SZ"]["price_source"], "sina_watch_batch")
        self.assertEqual(capture.quotes["000002.SZ"]["flow_source"], "eastmoney")
        self.assertNotIn("percentiles", calls)

    def test_volume_fallback_only_runs_when_the_all_a_snapshot_supplied_none(self):
        """The batched realtime quote is a failure path, not the normal one."""
        async def all_a_snapshot():
            return [], {"status": "unavailable"}

        async def direct(*_args, **_kwargs):
            return [{"symbol": "000001.SZ", "close": 10.0, "pre_close": 9.9}]

        async def sina(_symbols):
            return []

        async def eastmoney(_symbols, **_kwargs):
            return []

        async def reference(_symbols, _observed_at):
            return {"000001.SZ": {"float_shares": 100_000_000.0, "mean_daily_volume_shares": 4_800_000.0}}

        used = []

        async def volume_fallback(symbols):
            used.append(tuple(symbols))
            return {"000001.SZ": 2_000_000.0}

        calls = []
        dependencies = self.dependencies(
            all_a_snapshot=all_a_snapshot, tencent_watch_quotes=direct, sina_quotes=sina,
            eastmoney_watch_flows=eastmoney, calls=calls, watch_flow_reference=reference,
            volume_fallback=volume_fallback,
            derive_flow_metrics=lambda quotes, ref, *, observed_at: pure_derive_watch_flow_metrics(
                quotes, ref, observed_at=observed_at,
                number=lambda value: float(value) if value is not None else None,
            ),
        )
        capture = asyncio.run(capture_watch_quotes(
            ["000001.SZ"], datetime(2026, 8, 26, 5, 30, tzinfo=timezone.utc), 20.0, dependencies,
        ))
        quote = capture.quotes["000001.SZ"]
        self.assertEqual(used, [("000001.SZ",)], "fallback must run when all-A gave no volume")
        self.assertEqual(quote["volume_source"], "promax_rt_k_batch")
        self.assertAlmostEqual(quote["turnover_rate"], 2.0, places=5)
        self.assertEqual(capture.derived_flow_status["volume_fallback_symbols"], 1)

    def test_derived_metrics_replace_eastmoney_values_and_leave_main_flow_to_eastmoney(self):
        """THS-derived is primary; Eastmoney remains the fallback and sole main-flow source."""
        async def all_a_snapshot():
            return [{"symbol": "000001.SZ", "volume": 2_000_000.0}], {"status": "fresh", "cross_sectional": True}

        async def direct(*_args, **_kwargs):
            return []

        async def sina(_symbols):
            return []

        async def eastmoney(_symbols, **_kwargs):
            return [{"symbol": "000001.SZ", "volume_ratio": 1.90, "turnover_rate": 2.05,
                     "main_net_inflow": 12.0}]

        async def reference(_symbols, _observed_at):
            return {"000001.SZ": {"float_shares": 100_000_000.0, "mean_daily_volume_shares": 4_800_000.0}}

        def merge_eastmoney_flows(quotes, rows):
            for row in rows:
                quotes[row["symbol"]].update(
                    {key: row[key] for key in ("volume_ratio", "turnover_rate", "main_net_inflow")},
                )

        calls = []
        dependencies = self.dependencies(
            all_a_snapshot=all_a_snapshot, tencent_watch_quotes=direct, sina_quotes=sina,
            eastmoney_watch_flows=eastmoney, calls=calls, watch_flow_reference=reference,
            derive_flow_metrics=lambda quotes, ref, *, observed_at: pure_derive_watch_flow_metrics(
                quotes, ref, observed_at=observed_at,
                number=lambda value: float(value) if value is not None else None,
            ),
        )
        dependencies = dataclasses.replace(dependencies, merge_eastmoney_flows=merge_eastmoney_flows)
        capture = asyncio.run(capture_watch_quotes(
            ["000001.SZ"], datetime(2026, 8, 26, 3, 8, tzinfo=timezone.utc), 20.0, dependencies,
        ))
        quote = capture.quotes["000001.SZ"]
        # 11:08 CST -> 98 elapsed minutes of a 240-minute session.
        self.assertAlmostEqual(quote["turnover_rate"], 2.0, places=5)
        self.assertAlmostEqual(quote["volume_ratio"], (2_000_000 / 98) / (4_800_000 / 240), places=5)
        self.assertEqual(quote["turnover_rate_eastmoney_observed"], 2.05)
        self.assertEqual(quote["flow_metric_sources"],
                         {"volume_ratio": "fuyao_ths_derived", "turnover_rate": "fuyao_ths_derived",
                          "main_net_inflow": "eastmoney_watch_flow"})
        self.assertEqual(quote["main_net_inflow"], 12.0, "main flow has no licensed substitute")
        status = capture.derived_flow_status
        self.assertEqual(status["status"], "fresh")
        self.assertEqual(status["derived_field_symbols"], {"volume_ratio": 1, "turnover_rate": 1})
        self.assertEqual(status["eastmoney_agreement"]["turnover_rate"]["compared_symbols"], 1)

    def test_reference_failure_degrades_to_eastmoney_only_behaviour(self):
        async def all_a_snapshot():
            return [{"symbol": "000001.SZ", "volume": 2_000_000.0}], {"status": "fresh", "cross_sectional": True}

        async def direct(*_args, **_kwargs):
            return []

        async def sina(_symbols):
            return []

        async def eastmoney(_symbols, **_kwargs):
            return [{"symbol": "000001.SZ", "main_net_inflow": 12.0}]

        async def broken_reference(_symbols, _observed_at):
            raise ValueError("reference read failed")

        capture = asyncio.run(capture_watch_quotes(
            ["000001.SZ"], datetime(2026, 8, 26, 3, 8, tzinfo=timezone.utc), 20.0,
            self.dependencies(
                all_a_snapshot=all_a_snapshot, tencent_watch_quotes=direct, sina_quotes=sina,
                eastmoney_watch_flows=eastmoney, calls=[], watch_flow_reference=broken_reference,
            ),
        ))
        self.assertEqual(capture.derived_flow_status["status"], "unavailable")
        self.assertEqual(capture.derived_flow_status["error"], "reference read failed")
        self.assertNotIn("flow_metric_sources", capture.quotes["000001.SZ"])


if __name__ == "__main__":
    unittest.main()
