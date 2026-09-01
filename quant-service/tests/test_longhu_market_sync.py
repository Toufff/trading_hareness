from __future__ import annotations

import unittest
from datetime import date

from app.longhu_market_sync import build_control_rows, merge_cross_section


class LonghuMarketSyncTests(unittest.TestCase):
    def test_merge_requires_same_date_tencent_ohlc_and_preserves_flow(self):
        vendor = {
            "600664.SH": {
                "symbol": "600664.SH", "name": "哈药股份", "close": 9.49,
                "main_net": 83_000_000, "turnover_rate": 8.65, "volume_ratio": 1.22,
                "pe": 18.6, "pb": 2.4, "total_mv": 25_000_000_000,
                "circ_mv": 20_000_000_000, "raw": {"vendor": True},
            }
        }
        quotes = [{
            "ts_code": "600664.SH", "name": "哈药股份", "trade_date": "20260901",
            "open": 9.3, "high": 9.58, "low": 9.18, "close": 9.49,
            "pre_close": 9.29, "vol": 123456, "amount": 1_250_005_000,
        }]
        result = merge_cross_section(date(2026, 9, 1), vendor, quotes)
        self.assertEqual(result.coverage, 1.0)
        self.assertEqual(result.daily_rows[0]["close"], 9.49)
        self.assertEqual(result.flow_rows[0]["net_amount"], 83_000_000)
        self.assertEqual(result.quote_rows[0]["provider_basis"], "longhuvip+tencent")

    def test_control_rows_use_transparent_identity_factor_and_board_limits(self):
        daily = [
            {"ts_code": "600664.SH", "trade_date": "20260901", "pre_close": 10, "name": "哈药股份"},
            {"ts_code": "300001.SZ", "trade_date": "20260901", "pre_close": 10, "name": "特锐德"},
            {"ts_code": "600001.SH", "trade_date": "20260901", "pre_close": 10, "name": "ST测试"},
        ]
        controls = build_control_rows(daily)
        by_symbol = {row["ts_code"]: row for row in controls["stk_limit"]}
        self.assertEqual(by_symbol["600664.SH"]["up_limit"], "11.00")
        self.assertEqual(by_symbol["300001.SZ"]["up_limit"], "12.00")
        self.assertEqual(by_symbol["600001.SH"]["up_limit"], "10.50")
        self.assertTrue(all(row["factor_semantics"] == "same_day_identity_only" for row in controls["adj_factor"]))


if __name__ == "__main__":
    unittest.main()
