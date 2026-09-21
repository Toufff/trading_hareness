from datetime import datetime, timezone
import unittest

from app.broker_close_sync import (Capture, CloseSyncError, alias_matches, build_envelope, click_target, exchange_symbol,
                                   navigation_pane)
from app.broker_manual_sync import validate_manual_request

SH_CLOSE = datetime(2026, 9, 17, 7, 10, tzinfo=timezone.utc)  # 15:10 Shanghai


def read(**changes):
    value = {
        "page": "funds_holdings", "nav": {"资金股份": {"x": 88, "y": 349}, "查询": {"x": 70, "y": 321}},
        "client_alias": "中信证券-应*药", "list_complete": True,
        "account": {"funds_balance": "645.32", "available": "62.97", "withdrawable": "62.97", "frozen": "0.00",
                    "stock_market_value": "94313.00", "total_asset": "98911.83", "holding_pnl": "-3523.86",
                    "day_pnl": "-2733.60", "day_pnl_ratio": "-2.69%"},
        "rows": [
            {"code": "600664", "name": "哈药股份", "balance": "2300", "actual_quantity": "100", "sellable": "0", "frozen": "0.00",
             "cost": "-6.0257", "price": "7.4200", "floating_pnl": "1339.190", "day_pnl": "410.00"},
            {"code": "603823", "name": "百合花", "balance": "0", "actual_quantity": "500", "sellable": "0", "frozen": "0.00",
             "cost": "57.6832", "price": "57.6000", "floating_pnl": "-41.600", "day_pnl": "-41.60"},
            {"code": "000977", "name": "浪潮信息", "balance": "200", "actual_quantity": "200", "sellable": "200", "frozen": "0.00",
             "cost": "70.0950", "price": "69.8100", "floating_pnl": "-56.980", "day_pnl": "-268.00"},
            {"code": "002185", "name": "华天科技", "balance": "2900", "actual_quantity": "3100", "sellable": "400", "frozen": "0.00",
             "cost": "16.7400", "price": "16.3900", "floating_pnl": "-1084.210", "day_pnl": "-2174.00"},
            {"code": "002212", "name": "天融信", "balance": "3000", "actual_quantity": "0", "sellable": "0", "frozen": "0.00",
             "cost": "0.0000", "price": "7.1400", "floating_pnl": "-3680.260", "day_pnl": "-660.00"},
        ],
        "notes": "",
    }
    value.update(changes)
    return value


class CloseSyncTests(unittest.TestCase):
    def envelope(self, reads, prior=None):
        capture = Capture("G:\\e\\holdings.png", "A" * 64, "G:\\e\\holdings.png.receipt.json",
                          {"observed_at": "2026-09-17T07:10:00Z", "targetPID": 1, "targetHWND": 2})
        return build_envelope(run={"run_id": "r", "account_key": "citics-primary", "account_binding": {"method": "user_confirmed_once"}},
                              reads=reads, capture=capture, trade_date="2026-09-17", clicks=[],
                              prior_identity=prior or {"broker": "中信证券", "current_client_alias": "中信证券-应*药"})

    def test_only_trading_day_close_window_is_accepted(self):
        validate_manual_request("scheduled_close", False, "a", now=SH_CLOSE, calendar_open=True)
        for now, open_, code in [(SH_CLOSE, False, "NOT_TRADING_DAY"),
                                 (datetime(2026, 9, 17, 6, 50, tzinfo=timezone.utc), True, "OUTSIDE_WINDOW"),
                                 (datetime(2026, 9, 17, 7, 41, tzinfo=timezone.utc), True, "OUTSIDE_WINDOW"),
                                 (datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc), True, "OUTSIDE_WINDOW")]:
            with self.subTest(now=now), self.assertRaisesRegex(ValueError, code):
                validate_manual_request("scheduled_close", False, "a", now=now, calendar_open=open_)
        with self.assertRaisesRegex(ValueError, "MANUAL_ONLY"):
            validate_manual_request("midday", True, "a")

    def test_envelope_matches_the_manual_reading_of_the_same_screen(self):
        value = self.envelope([read(), read()])
        self.assertEqual(value["trigger"], "scheduled_close")
        self.assertEqual(value["account"], {"cash": "62.97", "total_asset": "98911.83", "total_market_value": "94313.00"})
        rows = {p["symbol"]: p for p in value["positions"]}
        self.assertEqual(sorted(rows), ["000977.SZ", "002185.SZ", "600664.SH", "603823.SH"])
        self.assertEqual(rows["600664.SH"]["average_cost"], "-6.0257")
        self.assertNotIn("average_cost_main_field_null_reason", rows["600664.SH"]["metadata"])
        self.assertEqual((rows["002185.SZ"]["quantity"], rows["002185.SZ"]["sellable_quantity"]), ("3100", "400"))
        self.assertEqual(value["completeness"]["zero_actual_rows_excluded"], 1)
        self.assertEqual(value["evidence"][0]["sha256"], "a" * 64)

    def test_disagreeing_misread_or_incomplete_reads_fail(self):
        misread = read()
        misread["rows"][3]["sellable"] = "4000"
        cases = [
            ([read(), misread], "READS_DISAGREE"),
            ([read(list_complete=False), read(list_complete=False)], "LIST_INCOMPLETE"),
            ([read(page="other", account=None)] * 2, "NOT_ON_FUNDS_PAGE"),
        ]
        wrong_price = read()
        wrong_price["rows"][1]["price"] = "57.5000"
        cases.append(([wrong_price, wrong_price], "MARKET_VALUE_MISMATCH"))
        for reads, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(CloseSyncError, code):
                self.envelope(reads)
        with self.assertRaisesRegex(CloseSyncError, "IDENTITY"):
            self.envelope([read(client_alias="华泰证券-王*五")] * 2)

    def test_clicks_stay_in_the_navigation_tree(self):
        windows = [{"pid": 7, "visible": True, "title": "HexinScrollWnd2",
                    "rect": {"left": 1447, "top": 75, "right": 1635, "bottom": 598, "width": 188, "height": 523}},
                   {"pid": 7, "visible": True, "title": "HexinScrollWnd2",
                    "rect": {"left": 1652, "top": 244, "right": 2555, "bottom": 584, "width": 903, "height": 340}}]
        pane = navigation_pane(windows, {"pid": 7, "rect": {"left": 1442, "top": 0}})
        self.assertEqual(pane, {"left": 5, "top": 75, "right": 193, "bottom": 598})
        self.assertEqual(click_target(read(page="other"), pane), ("资金股份", 88, 349))
        self.assertEqual(click_target(read(page="other", nav={"资金股份": None, "查询": {"x": 70, "y": 321}}), pane), ("查询", 70, 321))
        for nav, code in [({"资金股份": {"x": 400, "y": 349}, "查询": None}, "OUTSIDE_TREE"),
                          ({"资金股份": {"x": 88, "y": 20}, "查询": None}, "OUTSIDE_TREE"),
                          ({"资金股份": {"x": 88, "y": 100}, "查询": {"x": 70, "y": 321}}, "REJECTED"),
                          ({"资金股份": None, "查询": None}, "NOT_VISIBLE")]:
            with self.subTest(nav=nav), self.assertRaisesRegex(CloseSyncError, code):
                click_target(read(page="other", nav=nav), pane)

    def test_symbols_and_alias(self):
        self.assertEqual([exchange_symbol(c) for c in ("600664", "688001", "000977", "300750", "830799", "920001")],
                         ["600664.SH", "688001.SH", "000977.SZ", "300750.SZ", "830799.BJ", "920001.BJ"])
        self.assertTrue(alias_matches("中信证券-应*锜", "中信证券-应*药"))
        self.assertFalse(alias_matches("中信证券-王*药", "中信证券-应*药"))


if __name__ == "__main__":
    unittest.main()
