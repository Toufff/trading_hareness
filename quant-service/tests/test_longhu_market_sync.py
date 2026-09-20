from __future__ import annotations

import unittest
from datetime import date

from app.longhu_market_sync import build_control_rows, merge_cross_section


class LonghuMarketSyncTests(unittest.TestCase):
    def test_merge_requires_same_date_longhu_ohlc_and_preserves_flow(self):
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
        # vol stays lots; the canonical amount is thousand CNY while the raw quote keeps CNY.
        self.assertEqual((result.daily_rows[0]["vol"], result.daily_rows[0]["amount"]), (123456, "1250005"))
        self.assertEqual(result.quote_rows[0]["amount"], 1_250_005_000)
        self.assertEqual(result.flow_rows[0]["net_amount"], 83_000_000)
        self.assertEqual(result.quote_rows[0]["provider_basis"], "longhuvip_licensed_dated_ohlc")

    def test_control_rows_carry_board_limits_and_no_adjustment_factor_key(self):
        # The vendor publishes no corporate-action history.  An identity
        # placeholder was promoted onto canonical bars exactly like a real
        # cumulative tushare factor, so the honest contract is to emit nothing.
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
        self.assertNotIn("adj_factor", controls)
        self.assertEqual(set(controls), {"stk_limit"})

    def test_star_market_st_name_keeps_20_percent_not_5(self):
        # A substring match on "ST" used to force any registration-board ST
        # name down to the mainboard's 5% band; the shared prefix check must
        # not do that.
        daily = [{"ts_code": "688009.SH", "trade_date": "20260901", "pre_close": 10, "name": "ST测试科创"}]
        controls = build_control_rows(daily)
        by_symbol = {row["ts_code"]: row for row in controls["stk_limit"]}
        self.assertEqual(by_symbol["688009.SH"]["up_limit"], "12.00")

    def test_beijing_new_92_prefix_is_30_percent(self):
        daily = [{"ts_code": "920819.BJ", "trade_date": "20260901", "pre_close": 10, "name": "示例北交所"}]
        controls = build_control_rows(daily)
        by_symbol = {row["ts_code"]: row for row in controls["stk_limit"]}
        self.assertEqual(by_symbol["920819.BJ"]["up_limit"], "13.00")

    def test_beijing_limits_are_one_tick_narrower_than_half_up(self):
        # The close lane used to round every board half-up.  On 2026-09-18
        # that put 17 of 29 Beijing up-limits and 14 down-limits a tick wide,
        # and the limit price is what decides whether a bar is sealed.
        daily = [{"ts_code": "920002.BJ", "trade_date": "20260918",
                  "pre_close": "49.92", "name": "示例北交所"}]
        row = build_control_rows(daily)["stk_limit"][0]
        self.assertEqual((row["up_limit"], row["down_limit"]), ("64.89", "34.95"))
        self.assertEqual(row["derivation"], "preclose_times_board_limit_ratio_with_board_rounding")

    def test_a_symbol_with_a_quote_but_no_plate_row_still_gets_a_bar(self):
        # The vendor's industry plates are its classification, not its listing
        # roster: on 2026-09-18 they carried 90 of 345 BSE names, and tying the
        # bar to plate membership had held the whole exchange at ~29 bars a day
        # since 2026-09-04.
        vendor = {"600664.SH": {"symbol": "600664.SH", "name": "哈药股份", "close": 9.49,
                                "main_net": 1, "raw": {}}}
        quotes = [
            {"ts_code": "600664.SH", "trade_date": "20260901", "close": 9.49, "open": 9.3,
             "high": 9.58, "low": 9.18, "pre_close": 9.29, "vol": 1, "amount": 1000},
            {"ts_code": "920002.BJ", "trade_date": "20260901", "close": 50.02, "open": 49.7,
             "high": 50.71, "low": 49.66, "pre_close": 49.92, "vol": 2, "amount": 2000},
        ]
        result = merge_cross_section(date(2026, 9, 1), vendor, quotes)

        self.assertEqual([row["ts_code"] for row in result.daily_rows], ["600664.SH", "920002.BJ"])
        self.assertEqual(result.off_plate_rows, 1)
        # Price only: absence of vendor evidence is not published as a measurement.
        self.assertEqual([row["ts_code"] for row in result.fundamental_rows], ["600664.SH"])
        self.assertEqual([row["symbol"] for row in result.flow_rows], ["600664.SH"])
        off_plate = [row for row in result.quote_rows if row["ts_code"] == "920002.BJ"][0]
        self.assertEqual(off_plate["coverage_note"], "off_plate_price_only_no_vendor_cross_section")
        # The coverage gate keeps measuring exactly what it measured before.
        self.assertEqual(result.coverage, 1.0)

    def test_an_off_plate_quote_cannot_be_mistaken_for_a_close_conflict(self):
        quotes = [{"ts_code": "920002.BJ", "trade_date": "20260901", "close": 50.02, "open": 49.7,
                   "high": 50.71, "low": 49.66, "pre_close": 49.92, "vol": 2, "amount": 2000}]
        result = merge_cross_section(date(2026, 9, 1), {}, quotes)
        self.assertEqual(result.close_conflicts, ())
        self.assertEqual(len(result.daily_rows), 1)
        self.assertEqual(result.coverage, 0.0)


if __name__ == "__main__":
    unittest.main()
