from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app.annual_daily_backfill import (
    CORE_DAILY_SPECS,
    HISTORICAL_BACKFILL_CONFIRMATION,
    HISTORICAL_DAILY_AVAILABILITY_BASIS,
    SECTOR_EVENT_SPECS,
    _persist_sector_flow,
    historical_daily_strategy_available_at,
    parser,
    request_key,
    validate_historical_backfill_confirmation,
    valid_rows,
    validate_range,
)
from app.universe_history import rebuild_historical_membership_from_canonical


class AnnualDailyBackfillTests(unittest.TestCase):
    def test_scope_contains_no_minute_or_realtime_api(self):
        api_names = {spec.api_name for spec in (*CORE_DAILY_SPECS, *SECTOR_EVENT_SPECS)}
        self.assertFalse(any("min" in name or name.startswith("rt_") for name in api_names))
        self.assertIn("daily", api_names)
        self.assertIn("moneyflow_cnt_ths", api_names)
        self.assertIn("top_list", api_names)

    def test_daily_prefers_verified_get_with_primary_fallback(self):
        daily = next(spec for spec in CORE_DAILY_SPECS if spec.api_name == "daily")
        self.assertEqual(daily.provider_names, ("super_get", "super_sdk", "primary"))
        self.assertEqual(daily.minimum_rows, 4_800)

    def test_daily_controls_prefer_the_verified_city_path_before_primary(self):
        by_api = {spec.api_name: spec for spec in CORE_DAILY_SPECS}
        for api_name in ("daily_basic", "adj_factor", "stk_limit", "suspend_d"):
            self.assertEqual(by_api[api_name].provider_names, ("super_sdk", "primary"))

    def test_completed_day_is_shared_across_provider_routes(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("def _completed_equivalent_exists", source)
        self.assertIn("WHERE capability=%s AND trade_date=%s AND status='completed'", source)

    def test_range_level_bootstrap_reuses_same_parameter_completion_across_providers(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("def _completed_reference_equivalent_exists", source)
        self.assertIn("metadata @> %s::jsonb", source)
        self.assertIn("if self._completed_reference_equivalent_exists(api_name, params):", source)

    def test_failed_provider_is_suppressed_only_within_the_current_batch(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("self._batch_suppressed_candidates", source)
        self.assertIn("batch-local failover active", source)
        self.assertIn("if suppressed is not None", source)
        self.assertIn("if provider.uses_super_get(spec.api_name):", source)
        self.assertIn("live preference for Super GET in a fresh batch", source)

    def test_historical_city_core_requests_are_split_at_the_observed_burst_boundary(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("HISTORICAL_SUPER_SDK_GROUP_COOLDOWN_SECONDS = 61", source)
        self.assertIn("CORE_DAILY_SPECS[:3]", source)
        self.assertIn("CORE_DAILY_SPECS[3:]", source)
        self.assertIn("await asyncio.sleep(HISTORICAL_SUPER_SDK_GROUP_COOLDOWN_SECONDS)", source)

    def test_stock_cross_section_filters_non_a_codes_and_wrong_dates(self):
        rows = valid_rows("stk_limit", [
            {"ts_code": "600000.SH", "trade_date": "20260814"},
            {"ts_code": "000001.SZ", "trade_date": "20260813"},
            {"ts_code": "510300.SH", "trade_date": "20260814"},
            {"ts_code": "000300.SHX", "trade_date": "20260814"},
        ], date(2026, 8, 14))
        self.assertEqual(rows, [{"ts_code": "600000.SH", "trade_date": "20260814"}])

    def test_request_key_is_stable_and_parameter_sensitive(self):
        first = request_key("tushare_primary", "daily", {"trade_date": "20260814"})
        same = request_key("tushare_primary", "daily", {"trade_date": "20260814"})
        other = request_key("tushare_primary", "daily", {"trade_date": "20260813"})
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)

    def test_range_is_bounded_to_one_year(self):
        validate_range(date(2025, 8, 15), date(2026, 8, 14))
        with self.assertRaisesRegex(ValueError, "capped"):
            validate_range(date(2025, 1, 1), date(2026, 8, 14))

    def test_calendar_validation_scales_to_the_requested_range(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("expected_calendar_rows = (self.end_date - self.start_date).days + 1", source)
        self.assertIn("provider_names=(\"super_sdk\", \"primary\")", source)
        self.assertIn("calendar produced no open days for the requested range", source)

    def test_range_level_reference_tables_have_audited_fallbacks(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("async def _bootstrap_reference", source)
        self.assertIn("failed through every declared provider", source)
        self.assertIn("A failed legacy route is retained in ``fetch_runs``", source)

    def test_historical_backfill_requires_explicit_operator_acknowledgement(self):
        with self.assertRaisesRegex(ValueError, "disabled by default"):
            validate_historical_backfill_confirmation(None)
        validate_historical_backfill_confirmation(HISTORICAL_BACKFILL_CONFIRMATION)

    def test_sector_events_can_be_explicitly_skipped_for_a_p2_daily_only_range(self):
        args = parser().parse_args([
            "--start-date", "2024-08-15", "--end-date", "2025-08-14",
            "--skip-sector-events", "--confirm-historical-backfill", HISTORICAL_BACKFILL_CONFIRMATION,
        ])
        self.assertTrue(args.skip_sector_events)

    def test_targeted_core_repair_can_skip_sector_events_and_index_downloads(self):
        args = parser().parse_args([
            "--start-date", "2025-06-13", "--end-date", "2025-08-14",
            "--skip-sector-events", "--skip-index",
            "--confirm-historical-backfill", HISTORICAL_BACKFILL_CONFIRMATION,
        ])
        self.assertTrue(args.skip_sector_events)
        self.assertTrue(args.skip_index)

    def test_daily_backfill_uses_explicit_conservative_shanghai_clock(self):
        observed = historical_daily_strategy_available_at(date(2024, 8, 16))
        self.assertEqual(observed, datetime(2024, 8, 16, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(HISTORICAL_DAILY_AVAILABILITY_BASIS, "assumed_eod_1700_asia_shanghai_v1")

    def test_daily_aggregate_contract_keeps_tushare_units_explicit(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("total_amount_kcny", source)
        self.assertIn("total_volume_lots", source)
        self.assertIn("canonical_daily_multi_provider", source)
        self.assertNotIn("rt_min", source)

    def test_sector_promotion_preserves_eight_digit_regex_in_sql(self):
        class RecordingConnection:
            def __init__(self): self.calls = []
            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        connection = RecordingConnection()
        _persist_sector_flow(
            connection, "tushare_super_sdk",
            datetime.now(timezone.utc),
            kind="industry_flow",
        )
        observation_sql = next(sql for sql, _ in connection.calls if "sector_market_observations" in sql)
        self.assertIn(r"^\d{8}$", observation_sql)
        self.assertIn("SELECT DISTINCT ON(row_data->>'ts_code',row_data->>'trade_date')", observation_sql)

    def test_raw_bulk_insert_deduplicates_supplier_duplicates(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("SELECT DISTINCT ON(record_key,content_sha256)", source)
        self.assertIn("SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date')", source)

    def test_canonical_and_control_promotions_deduplicate_same_symbol_day(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        # Retain duplicate raw vendor payloads for audit, but never submit two
        # versions of one symbol/day to a single canonical/control UPSERT.
        self.assertGreaterEqual(
            source.count("SELECT DISTINCT ON(upper(row_data->>'ts_code'),row_data->>'trade_date') row_data"),
            5,
        )
        self.assertIn("record_index DESC", source)
        self.assertIn("SELECT DISTINCT ON (upper(s.row_data->>'ts_code'),to_date(s.row_data->>'trade_date','YYYYMMDD'))", source)

    def test_concurrent_core_lane_has_final_control_reconciliation(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("await asyncio.gather", source)
        self.assertIn("FROM quant.daily_adjustment_factors", source)
        self.assertIn("FROM quant.daily_trade_limits", source)

    def test_control_reconciliation_uses_the_city_fallback_when_primary_is_unavailable(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        reconciliation = source[source.index("def reconcile_suspensions"):source.index("def promote_stored_sector_flows")]
        self.assertIn("WHEN 'tushare_super_sdk' THEN 0", reconciliation)
        self.assertGreaterEqual(reconciliation.count("bar.trading_date BETWEEN %s AND %s"), 3)
        self.assertNotIn("factor.provider='tushare_primary'", reconciliation)
        self.assertNotIn("limits.provider='tushare_primary'", reconciliation)

    def test_reprojection_is_local_only_and_preserves_dual_clock_evidence(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("def reproject_stored_historical_clocks", source)
        self.assertIn("provider_requests\": 0", source)
        self.assertIn("ingested_at=coalesce", source)
        self.assertIn("availability_basis=EXCLUDED.availability_basis", source)
        reproject_source = source[source.index("def reproject_stored_historical_clocks"):source.index("async def run")]
        self.assertNotIn("_persist_raw(", reproject_source)
        self.assertIn("UPDATE quant.tushare_raw_records", reproject_source)
        self.assertIn("payload = valid_rows(spec.api_name", reproject_source)

    def test_projection_lookup_index_is_versioned_for_future_deployments(self):
        migration = (Path("migrations/versions/20260816_0050_annual_projection_lookup_index.py")
                     .read_text(encoding="utf-8"))
        self.assertIn("tushare_raw_provider_api_trade_date_idx", migration)
        self.assertIn("provider_key, api_name", migration)

    def test_historical_universe_projection_retains_inferred_delisting_rows(self):
        class Result:
            def __init__(self, rowcount): self.rowcount = rowcount

        class Connection:
            def __init__(self): self.calls = []
            def execute(self, sql, params):
                self.calls.append((sql, params))
                return Result(7 if "INSERT INTO" in sql else 3)

        connection = Connection()
        result = rebuild_historical_membership_from_canonical(connection)
        self.assertEqual(result, {"removed": 3, "inserted": 7})
        projection_sql = connection.calls[-1][0]
        self.assertIn("annual_daily_backfill_pit_inferred_delisting", projection_sql)
        self.assertIn("supplier_delist_date_or_last_bar", projection_sql)
        self.assertIn("current_active_snapshot", projection_sql)


if __name__ == "__main__":
    unittest.main()
