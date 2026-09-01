"""Bounded minute-backfill pipeline ordering and failure isolation."""

from provider_test_support import *  # noqa: F403


def _full_market_request(*, trade_date=None):
    return {"trade_date": trade_date}


class MinuteBarBackfillRunsLastAndNeverFailsThePipelineTests(unittest.TestCase):
    """The minute-bar pass is best-effort supplementary data."""

    def _pipeline(self, backfill, *, budget=None, order=None):
        async def tracked(operation, *_a, **_k):
            if order is not None:
                order.append("db")
            return {"status": "ready"}

        async def wrapped(as_of):
            if order is not None:
                order.append("backfill")
            return await backfill(as_of)

        kwargs = dict(
            sync_full_market_daily=AsyncMock(return_value={"status": "completed"}),
            sync_baostock=AsyncMock(return_value={"status": "completed"}),
            sync_full_market_daily_controls=AsyncMock(return_value={"status": "completed"}),
            tushare_request=TushareSyncRequest, full_market_request=_full_market_request,
            snapshot_request=lambda a: {"as_of_date": a}, build_snapshot=object(),
            recompute_outcomes=object(), recompute_scorecards=object(),
            generate_recommendations=object(), run_database_blocking=tracked,
            cn_today=lambda: date(2026, 8, 28), backfill_minute_bars=wrapped)

        def go():
            return asyncio.run(run_pipeline(GenerateRequest(as_of_date=date(2026, 8, 28)), **kwargs))

        if budget is not None:
            with patch("app.daily_pipeline.MINUTE_BACKFILL_BUDGET_SECONDS", budget):
                return go()
        return go()

    def test_the_backfill_runs_after_the_decision_stages(self):
        order = []

        async def ok(_a):
            return {"availability_pct": 55.0, "bars": 2885}

        result = self._pipeline(ok, order=order)
        self.assertEqual(order[-1], "backfill")
        self.assertEqual(result["minute_bars"], {"availability_pct": 55.0, "bars": 2885})

    def test_a_failing_backfill_leaves_the_pipeline_completed(self):
        async def boom(_a):
            raise RuntimeError("stk_mins 202")

        result = self._pipeline(boom)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["minute_bars"]["status"], "failed")
        self.assertIn("RuntimeError", result["minute_bars"]["error"])

    def test_a_slow_backfill_is_bounded_by_the_budget(self):
        async def hang(_a):
            await asyncio.sleep(1.0)

        result = self._pipeline(hang, budget=0.05)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["minute_bars"]["status"], "timeout")
        self.assertEqual(result["minute_bars"]["budget_seconds"], 0.05)

    def test_the_pipeline_completes_unchanged_without_a_backfill_wired(self):
        result = asyncio.run(run_pipeline(
            GenerateRequest(as_of_date=date(2026, 8, 28)),
            sync_full_market_daily=AsyncMock(return_value={"status": "completed"}),
            sync_baostock=AsyncMock(return_value={"status": "completed"}),
            sync_full_market_daily_controls=AsyncMock(return_value={"status": "completed"}),
            tushare_request=TushareSyncRequest, full_market_request=_full_market_request,
            snapshot_request=lambda a: {"as_of_date": a}, build_snapshot=object(),
            recompute_outcomes=object(), recompute_scorecards=object(),
            generate_recommendations=object(),
            run_database_blocking=AsyncMock(return_value={"status": "ready"}),
            cn_today=lambda: date(2026, 8, 28)))
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["minute_bars"])
