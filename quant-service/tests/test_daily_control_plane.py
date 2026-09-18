import asyncio
import unittest
from datetime import date
from unittest.mock import MagicMock

from app.daily_control_plane import (
    DailyControlPlaneSyncDependencies,
    EQUITY_DAILY_CONTROL_STATUS_SQL,
    GATED_EXCHANGES,
    UNGATED_EXCHANGES,
    daily_row_count,
    status_payload,
    sync_full_market_daily_controls,
)


def _row(exchange, expected, daily, *, adjustment=None, limit=None, vendor_sourced=0,
         trading_date=date(2026, 9, 18), previous_day=None, previous_expected=None, sources=None):
    return {
        "trading_date": trading_date, "exchange": exchange,
        "expected_daily_rows": expected, "daily_rows": daily,
        "adjustment_rows": daily if adjustment is None else adjustment,
        "limit_rows": daily if limit is None else limit,
        "vendor_sourced_rows": vendor_sourced,
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

    def test_missing_limit_controls_remain_fail_closed(self):
        payload = status_payload([_row("SH", 3_447, 3_447, limit=3_446)])
        self.assertEqual(payload["state"], "blocked")
        self.assertIn("missing same-date limit controls", payload["reason"])

    def test_missing_adjustment_no_longer_blocks_the_equity_gate_but_is_labelled(self):
        """Factors are a separate provider lane; a pending factor is not an outage.

        This is the contract that has to ship together with the ten-day-leader
        predicate change: the moment the identity placeholders become NULL,
        every vendor-sourced session would otherwise report ``blocked``.
        """
        payload = status_payload([
            _row("SH", 3_447, 3_447, adjustment=0, vendor_sourced=3_447)])
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["adjustment_state"], "pending")
        self.assertEqual(payload["adjustment_rows"], 0)
        self.assertEqual(payload["adjustment_pending_rows"], 3_447)
        self.assertFalse(payload["research_adjustment_ready"])
        self.assertIn("复权因子 pending", payload["reason"])

    def test_adjustment_is_absent_when_no_vendor_sourced_bar_explains_it(self):
        payload = status_payload([_row("SH", 3_447, 3_447, adjustment=3_400)])
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["adjustment_state"], "absent")
        self.assertEqual(payload["adjustment_pending_rows"], 47)
        self.assertIn("复权因子 absent", payload["reason"])

    def test_complete_adjustment_reports_research_ready_and_no_reason(self):
        payload = status_payload([_row("SH", 3_447, 3_447)])
        self.assertEqual(payload["adjustment_state"], "complete")
        self.assertTrue(payload["research_adjustment_ready"])
        self.assertIsNone(payload["reason"])

    def test_absent_payload_carries_the_tri_state_keys(self):
        payload = status_payload(None)
        self.assertEqual(payload["adjustment_state"], "absent")
        self.assertEqual(payload["adjustment_pending_rows"], 0)
        self.assertFalse(payload["research_adjustment_ready"])

    def test_status_sql_projects_the_vendor_sourced_count(self):
        from app.daily_control_plane import PROVIDERS_WITHOUT_ADJUSTMENT_FACTORS
        self.assertIn("vendor_sourced_rows", EQUITY_DAILY_CONTROL_STATUS_SQL)
        for provider in PROVIDERS_WITHOUT_ADJUSTMENT_FACTORS:
            self.assertIn(f"'{provider}'", EQUITY_DAILY_CONTROL_STATUS_SQL)

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

    def test_a_single_row_is_rejected_instead_of_scored_as_one_exchange(self):
        """A stale ``fetchone()`` caller must fail, not report a wrong verdict."""
        single = _row("BJ", 342, 0)
        with self.assertRaises(TypeError):
            status_payload(single)
        self.assertEqual(status_payload([single])["state"], "blocked")


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
            "expected": 342, "daily": 0, "adjustment": 0, "limit": 0, "vendor_sourced": 0,
            "gated": False, "ratio": 0.0})
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
        # The grouping counts membership rows starting that day (312+12=324), which
        # is not the net delta; the text must not read as if the two must balance.
        self.assertIn("（当日新增 324，来源分组："
                      "stock-basic-all-a:tushare_super_get 312, longhuvip_composite 12）",
                      payload["reason"])
        self.assertNotIn("+305（来源分组", payload["reason"])

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

    def test_only_the_ungated_exchanges_leave_the_gate(self):
        """The payload decides the gate by exclusion, exactly as daily_row_count does."""
        payload = status_payload([_row("SH", 10, 10), _row("BJ", 10, 0), _row(None, 10, 0)])
        self.assertEqual(
            {name: bucket["gated"] for name, bucket in payload["by_exchange"].items()},
            {"SH": True, "BJ": False, "UNKNOWN": True})
        self.assertEqual([item["exchange"] for item in payload["ungated_exchanges"]],
                         list(UNGATED_EXCHANGES))

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
        # A membership retired on the same day it started is not an addition.
        grouping = EQUITY_DAILY_CONTROL_STATUS_SQL.split('expected_sources AS (', 1)[1]
        self.assertIn("AND (membership.effective_to IS NULL"
                      " OR membership.effective_to>=latest.trading_date)",
                      grouping.split(') SELECT', 1)[0])

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

    def test_expected_population_excludes_only_the_ungated_exchanges(self):
        """Excluding BJ, rather than including SH/SZ, keeps an unsuffixed symbol
        inside both this count and ``status_payload``'s gate."""
        database = _fake_database({"expected_rows": 5221, "actual_rows": 5122})
        self.assertEqual(daily_row_count(database, date(2026, 9, 18)), 5122)
        connection = database.transaction.return_value.__enter__.return_value
        sql, params = connection.execute.call_args[0]
        self.assertEqual(sql.count('%s'), len(params))
        self.assertEqual([value for value in params if value == list(UNGATED_EXCHANGES)],
                         [list(UNGATED_EXCHANGES), list(UNGATED_EXCHANGES)])
        self.assertNotIn('ANY(%s)', sql)
        self.assertIn("coalesce(nullif(upper(split_part(symbol,'.',2)),''),'UNKNOWN')<>ALL(%s)", sql)
        self.assertIn(
            "coalesce(nullif(upper(split_part(bar.symbol,'.',2)),''),'UNKNOWN')<>ALL(%s)", sql)
        self.assertEqual(sorted(set(GATED_EXCHANGES) & set(UNGATED_EXCHANGES)), [])


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

    def test_vendor_short_circuit_no_longer_needs_factor_rows(self):
        """The vendor writes no factor rows at all now; the gate is per control.

        Keeping ``factor_rows`` in the gate would mean the short-circuit could
        never fire again, forcing a full four-API tushare sync every post-close
        through a currently failing adj_factor route.
        """
        async def run_database(action):
            return action()

        database = _fake_database({"daily_rows": 5_100, "factor_rows": 0, "limit_rows": 5_100})
        dependencies = self._dependencies(
            database=database, longhu_vendor_configured=lambda: True, run_database=run_database,
        )

        result = asyncio.run(sync_full_market_daily_controls(date(2026, 9, 18), dependencies))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["rows"]["adj_factor"], 0)
        self.assertEqual(result["satisfied_by_vendor"], ["stk_limit", "daily_basic"])
        self.assertEqual(result["pending_controls"], ["adj_factor"])
        self.assertEqual(result["adjustment_state"], "pending")
        self.assertNotIn("same-day identity", result["quality_note"])

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
