from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app.annual_daily_backfill import (
    CORE_DAILY_SPECS,
    SECTOR_EVENT_SPECS,
    _persist_sector_flow,
    request_key,
    valid_rows,
    validate_range,
)


class AnnualDailyBackfillTests(unittest.TestCase):
    def test_scope_contains_no_minute_or_realtime_api(self):
        api_names = {spec.api_name for spec in (*CORE_DAILY_SPECS, *SECTOR_EVENT_SPECS)}
        self.assertFalse(any("min" in name or name.startswith("rt_") for name in api_names))
        self.assertIn("daily", api_names)
        self.assertIn("moneyflow_cnt_ths", api_names)
        self.assertIn("top_list", api_names)

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

    def test_daily_aggregate_contract_keeps_tushare_units_explicit(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("total_amount_kcny", source)
        self.assertIn("total_volume_lots", source)
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

    def test_raw_bulk_insert_deduplicates_supplier_duplicates(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("SELECT DISTINCT ON(record_key,content_sha256)", source)

    def test_concurrent_core_lane_has_final_control_reconciliation(self):
        source = Path("app/annual_daily_backfill.py").read_text(encoding="utf-8")
        self.assertIn("await asyncio.gather", source)
        self.assertIn("FROM quant.daily_adjustment_factors factor", source)
        self.assertIn("FROM quant.daily_trade_limits limits", source)


if __name__ == "__main__":
    unittest.main()
