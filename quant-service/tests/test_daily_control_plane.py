import asyncio
import unittest
from datetime import date
from unittest.mock import MagicMock

from app.daily_control_plane import (
    DailyControlPlaneSyncDependencies,
    EQUITY_DAILY_CONTROL_STATUS_SQL,
    GATED_EXCHANGES,
    daily_row_count,
    status_payload,
    sync_full_market_daily_controls,
)


def _row(exchange, expected, daily, *, adjustment=None, limit=None,
         trading_date=date(2026, 9, 18), previous_day=None, previous_expected=None, sources=None):
    return {
        "trading_date": trading_date, "exchange": exchange,
        "expected_daily_rows": expected, "daily_rows": daily,
        "adjustment_rows": daily if adjustment is None else adjustment,
        "limit_rows": daily if limit is None else limit,
        "expected_previous_trading_day": previous_day,
        "expected_previous_daily_rows": previous_expected,
        "expected_sources": sources,
    }


class DailyControlPlaneTests(unittest.TestCase):
    def test_requested_date_query_does_not_use_latest_date(self):
        from app.daily_control_plane import status_query
        sql, params = status_query(date(2026, 9, 7))
        self.assertEqual(params, (date(2026, 9, 7),))
        self.assertNotIn('max(trading_date) AS trading_date FROM equity_bars', sql)
        self.assertIn('SELECT %s::date AS trading_date', sql)
        self.assertEqual(sql.count('%s'), len(params))
        self.assertEqual(status_query(), (EQUITY_DAILY_CONTROL_STATUS_SQL, ()))

    def test_index_rows_do_not_participate_in_equity_control_gate(self):
        self.assertIn("universe_key='all_a'", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("expected_daily_rows", EQUITY_DAILY_CONTROL_STATUS_SQL)
        payload = status_payload([_row("SH", 2_000, 1_980), _row("SZ", 3_000, 2_970)])
        self.assertEqual(payload["state"], "ready")
        self.assertIsNone(payload["reason"])
        self.assertEqual(payload["minimum_required_rows"], 4_750)

    def test_missing_equity_controls_remain_fail_closed(self):
        payload = status_payload([_row("SH", 3_447, 3_447, adjustment=3_446)])
        self.assertEqual(payload["state"], "blocked")
        self.assertIn("missing", payload["reason"])

    def test_incomplete_daily_cross_section_remains_blocked_even_with_complete_local_controls(self):
        payload = status_payload([_row("SH", 2_549, 1_447), _row("SZ", 3_000, 2_000)])
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["coverage_ratio"], 0.6212)
        self.assertIn("point-in-time all-A", payload["reason"])

    def test_empty_result_is_absent(self):
        payload = status_payload(None)
        self.assertEqual(payload['state'], 'absent')
        self.assertIsNone(payload['trade_date'])
        self.assertEqual(payload['daily_rows'], 0)
        self.assertEqual(payload['by_exchange'], {})
        self.assertEqual(payload['gating_exchanges'], list(GATED_EXCHANGES))
        self.assertIsNone(payload['expected_delta'])

    def test_null_aggregate_date_is_absent_not_string_none(self):
        payload = status_payload([{'trading_date': None, 'daily_rows': 0}])
        self.assertIsNone(payload['trade_date'])
        self.assertEqual(payload['state'], 'absent')


class PerExchangeGateTests(unittest.TestCase):
    """The 2026-09-18 regression: 312 BJ codes joined all_a with no BJ daily source."""

    def _september_eighteenth(self):
        sources = {"stock-basic-all-a:tushare_super_get": 312, "longhuvip_composite": 12}
        return [
            _row("BJ", 342, 0, previous_day=date(2026, 9, 17), previous_expected=5_258,
                 sources=sources),
            _row("SH", 2_300, 2_260, previous_day=date(2026, 9, 17), previous_expected=5_258,
                 sources=sources),
            _row("SZ", 2_921, 2_862, previous_day=date(2026, 9, 17), previous_expected=5_258,
                 sources=sources),
        ]

    def test_beijing_without_a_daily_source_does_not_block_a_complete_sh_sz_session(self):
        payload = status_payload(self._september_eighteenth())
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["gating_exchanges"], ["SH", "SZ"])
        self.assertEqual(payload["daily_rows"], 5_122)
        self.assertEqual(payload["expected_daily_rows"], 5_221)
        self.assertEqual(payload["coverage_ratio"], 0.981)
        self.assertEqual(payload["all_a"], {"expected_daily_rows": 5_563, "daily_rows": 5_122})
        self.assertEqual(payload["by_exchange"]["BJ"], {
            "expected": 342, "daily": 0, "adjustment": 0, "limit": 0, "gated": False, "ratio": 0.0})
        self.assertEqual(payload["ungated_exchanges"],
                         [{"exchange": "BJ", "expected": 342, "daily": 0, "ratio": 0.0}])

    def test_gated_exchange_below_the_ratio_still_blocks(self):
        rows = self._september_eighteenth()
        rows[2] = _row("SZ", 2_921, 2_000, previous_day=date(2026, 9, 17),
                       previous_expected=5_258, sources=None)
        payload = status_payload(rows)
        self.assertEqual(payload["state"], "blocked")
        self.assertIn("SH+SZ 4260/5221", payload["reason"])
        self.assertIn("低于 95%", payload["reason"])
        self.assertIn("BJ 0/342 未参与门槛", payload["reason"])

    def test_universe_drift_above_two_percent_is_named_with_its_source_grouping(self):
        payload = status_payload(self._september_eighteenth())
        self.assertEqual(payload["expected_previous_trading_day"], "2026-09-17")
        self.assertEqual(payload["expected_previous_daily_rows"], 5_258)
        self.assertEqual(payload["expected_delta"], 305)
        self.assertEqual(payload["expected_sources"],
                         {"stock-basic-all-a:tushare_super_get": 312, "longhuvip_composite": 12})
        self.assertIn("SH+SZ 5122/5221=98.1% ready", payload["reason"])
        self.assertIn("all_a 预期较上一交易日 +305", payload["reason"])
        self.assertIn("来源分组：stock-basic-all-a:tushare_super_get 312, longhuvip_composite 12",
                      payload["reason"])

    def test_ordinary_daily_churn_is_not_reported_as_drift(self):
        payload = status_payload([
            _row("SH", 2_300, 2_290, previous_day=date(2026, 9, 16), previous_expected=5_200,
                 sources={"longhuvip_composite": 21}),
            _row("SZ", 2_921, 2_910, previous_day=date(2026, 9, 16), previous_expected=5_200,
                 sources={"longhuvip_composite": 21}),
        ])
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["expected_delta"], 21)
        self.assertIsNone(payload["reason"])

    def test_unknown_exchange_suffix_stays_inside_the_gate(self):
        payload = status_payload([_row("SH", 2_300, 2_290), _row(None, 300, 0)])
        self.assertTrue(payload["by_exchange"]["UNKNOWN"]["gated"])
        self.assertEqual(payload["ungated_exchanges"], [])
        self.assertEqual(payload["expected_daily_rows"], 2_600)
        self.assertEqual(payload["state"], "blocked")

    def test_missing_previous_session_leaves_the_delta_unknown_instead_of_zero(self):
        payload = status_payload([_row("SH", 2_300, 2_290), _row("SZ", 2_921, 2_910)])
        self.assertIsNone(payload["expected_previous_trading_day"])
        self.assertIsNone(payload["expected_delta"])
        self.assertEqual(payload["expected_sources"], {})
        self.assertIsNone(payload["reason"])


class EquityStatusSqlShapeTests(unittest.TestCase):
    """The SQL is asserted on its parameters and grouping keys, never on a live database."""

    def test_sql_groups_expected_and_observed_rows_by_exchange(self):
        self.assertIn("split_part(bar.symbol,'.',2)", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("split_part(membership.symbol,'.',2)", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("bar.exchange=expected.exchange", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("GROUP BY latest.trading_date,2", EQUITY_DAILY_CONTROL_STATUS_SQL)

    def test_sql_derives_the_previous_session_and_its_source_grouping(self):
        self.assertIn("prior.trading_date<latest.trading_date", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("expected_previous_trading_day", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("membership.effective_from=latest.trading_date", EQUITY_DAILY_CONTROL_STATUS_SQL)
        self.assertIn("jsonb_object_agg(grouped.source,grouped.symbols)", EQUITY_DAILY_CONTROL_STATUS_SQL)

    def test_status_sql_takes_no_parameters_until_a_date_is_requested(self):
        self.assertNotIn('%s', EQUITY_DAILY_CONTROL_STATUS_SQL)


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

    def test_expected_population_is_restricted_to_the_gated_exchanges(self):
        database = _fake_database({"expected_rows": 5221, "actual_rows": 5122})
        self.assertEqual(daily_row_count(database, date(2026, 9, 18)), 5122)
        connection = database.transaction.return_value.__enter__.return_value
        sql, params = connection.execute.call_args[0]
        self.assertEqual(sql.count('%s'), len(params))
        self.assertEqual([value for value in params if value == list(GATED_EXCHANGES)],
                         [list(GATED_EXCHANGES), list(GATED_EXCHANGES)])
        self.assertIn("upper(split_part(symbol,'.',2))=ANY(%s)", sql)
        self.assertIn("upper(split_part(bar.symbol,'.',2))=ANY(%s)", sql)


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
