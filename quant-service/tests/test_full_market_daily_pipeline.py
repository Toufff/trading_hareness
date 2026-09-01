"""Full-market daily pipeline request and fallback boundaries."""

from provider_test_support import *  # noqa: F403


def _full_market_request(*, trade_date=None):
    return {"trade_date": trade_date}


class DailyPipelineTakesTheCrossSectionInOneRequestTests(unittest.TestCase):
    """The market stages must not iterate the universe symbol by symbol."""

    def _run(self, *, primary_status="completed"):
        seen = {}

        async def sync_full_market_daily(request):
            seen["request"] = request
            return {"status": primary_status}

        async def sync_baostock(_request):
            seen["baostock"] = True
            return {"status": "completed"}

        controls = AsyncMock(return_value={"status": "completed"})

        async def blocking(operation, *_args, **_kwargs):
            return {"status": "ready"} if operation is build else {}

        build = object()
        asyncio.run(run_pipeline(
            GenerateRequest(as_of_date=date(2026, 8, 27)),
            sync_full_market_daily=sync_full_market_daily, sync_baostock=sync_baostock,
            sync_full_market_daily_controls=controls,
            tushare_request=TushareSyncRequest, full_market_request=_full_market_request,
            snapshot_request=lambda as_of: {"as_of_date": as_of}, build_snapshot=build,
            recompute_outcomes=object(), recompute_scorecards=object(),
            generate_recommendations=object(), run_database_blocking=blocking,
            cn_today=lambda: date(2026, 8, 27),
        ))
        return seen, controls

    def test_the_daily_stage_asks_for_one_whole_trade_date(self):
        seen, _ = self._run()
        self.assertEqual(seen["request"], {"trade_date": date(2026, 8, 27)},
                         "the stage must request a trade date, never a single symbol")

    def test_controls_run_on_the_resolved_date_not_none(self):
        _, controls = self._run()
        controls.assert_awaited_once_with(date(2026, 8, 27))

    def test_a_blocked_primary_falls_back_rather_than_settling_on_a_partial_date(self):
        seen, controls = self._run(primary_status="blocked")
        self.assertTrue(seen.get("baostock"), "a blocked cross-section must reach the fallback")
        controls.assert_not_awaited()
