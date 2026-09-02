import asyncio
import unittest
from datetime import date
from unittest.mock import MagicMock

from app.daily_control_plane import (
    DailyControlPlaneSyncDependencies,
    EQUITY_DAILY_CONTROL_STATUS_SQL,
    daily_row_count,
    status_payload,
    sync_full_market_daily_controls,
)


class DailyControlPlaneTests(unittest.TestCase):
    def test_index_rows_do_not_participate_in_equity_control_gate(self):
        self.assertIn("universe_key='all_a'", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("expected_daily_rows", EQUITY_DAILY_CONTROL_STATUS_SQL)
        payload = status_payload({
            "trading_date": date(2026, 8, 21), "expected_daily_rows": 5_000,
            "daily_rows": 4_950, "adjustment_rows": 4_950, "limit_rows": 4_950,
        })
        self.assertEqual(payload["state"], "ready")
        self.assertIsNone(payload["reason"])
        self.assertEqual(payload["minimum_required_rows"], 4_750)

    def test_missing_equity_controls_remain_fail_closed(self):
        payload = status_payload({
            "trading_date": date(2026, 8, 21), "daily_rows": 3447,
            "adjustment_rows": 3446, "limit_rows": 3447,
        })
        self.assertEqual(payload["state"], "blocked")
        self.assertIn("missing", payload["reason"])

    def test_incomplete_daily_cross_section_remains_blocked_even_with_complete_local_controls(self):
        payload = status_payload({
            "trading_date": date(2026, 8, 21), "expected_daily_rows": 5_549,
            "daily_rows": 3_447, "adjustment_rows": 3_447, "limit_rows": 3_447,
        })
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["coverage_ratio"], 0.6212)
        self.assertIn("point-in-time all-A", payload["reason"])

    def test_empty_result_is_absent(self):
        self.assertEqual(status_payload(None), {"state": "absent", "reason": "no canonical equity daily bars"})


def _fake_database(row):
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = row
    database = MagicMock()
    database.transaction.return_value.__enter__.return_value = connection
    return database


class DailyRowCountTests(unittest.TestCase):
    def test_returns_zero_when_expected_population_is_unknown(self):
        database = _fake_database({"expected_rows": 0, "actual_rows": 0})
        self.assertEqual(daily_row_count(database, date(2026, 8, 21)), 0)

    def test_returns_actual_rows_once_the_coverage_ratio_is_met(self):
        database = _fake_database({"expected_rows": 5000, "actual_rows": 4800})
        self.assertEqual(daily_row_count(database, date(2026, 8, 21)), 4800)

    def test_returns_zero_when_coverage_falls_short(self):
        database = _fake_database({"expected_rows": 5000, "actual_rows": 4000})
        self.assertEqual(daily_row_count(database, date(2026, 8, 21)), 0)


class SyncFullMarketDailyControlsTests(unittest.TestCase):
    def _dependencies(self, **overrides):
        base = dict(
            database=MagicMock(), longhu_vendor_configured=lambda: False,
            run_database=None, call_tushare_api=None, parse_tushare_date=None,
            persist_tushare_rows=None, persist_blocked=None, safe_error_detail=None,
            executor_saturated_error=RuntimeError, record_provider_success=None,
            record_provider_failure=None, record_provider_api_capability=None,
        )
        base.update(overrides)
        return DailyControlPlaneSyncDependencies(**base)

    def test_longhu_ready_status_short_circuits_the_tushare_sync(self):
        async def run_database(action):
            return action()

        database = _fake_database({"daily_rows": 4000, "factor_rows": 4000, "limit_rows": 4000})
        dependencies = self._dependencies(
            database=database, longhu_vendor_configured=lambda: True, run_database=run_database,
        )

        result = asyncio.run(sync_full_market_daily_controls(date(2026, 8, 21), dependencies))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider"], "longhuvip_composite")

    def test_falls_through_to_tushare_sync_when_longhu_is_not_configured(self):
        called = {}

        async def call_tushare_api(*_args, **_kwargs):  # pragma: no cover - not reached in this test
            raise AssertionError("unexpected tushare call")

        import app.daily_control_plane as module

        async def fake_isolated(trade_date, **kwargs):
            called["trade_date"] = trade_date
            called["kwargs"] = kwargs
            return {"status": "completed", "provider": "tushare"}

        original = module.sync_full_market_daily_controls_isolated
        module.sync_full_market_daily_controls_isolated = fake_isolated
        try:
            dependencies = self._dependencies(call_tushare_api=call_tushare_api)
            result = asyncio.run(sync_full_market_daily_controls(date(2026, 8, 21), dependencies))
        finally:
            module.sync_full_market_daily_controls_isolated = original

        self.assertEqual(result, {"status": "completed", "provider": "tushare"})
        self.assertEqual(called["trade_date"], date(2026, 8, 21))
