from __future__ import annotations

import unittest
from datetime import date

from app.longhu_vendor_source import (
    MAX_PAGE_SIZE,
    LonghuVendorConfig,
    LonghuVendorSource,
    SharedLonghuReadSource,
    normalize_stock_symbol,
    parse_industry_stock_row,
    parse_stock_minute_payload,
    parse_stock_snapshot_payload,
    parse_tencent_quote_text,
    safe_page_size,
)


class LonghuVendorSourceTests(unittest.TestCase):
    def test_safe_page_size_never_exceeds_vendor_hard_limit(self):
        self.assertEqual(safe_page_size(1), 1)
        self.assertEqual(safe_page_size(300), MAX_PAGE_SIZE)
        self.assertEqual(safe_page_size(2_000), MAX_PAGE_SIZE)
        with self.assertRaises(ValueError):
            safe_page_size(0)

    def test_config_requires_complete_credentials(self):
        with self.assertRaises(ValueError):
            LonghuVendorConfig.from_mapping({"token": "x", "user_id": "", "device_id": "d"})

    def test_industry_row_preserves_vendor_flow_semantics(self):
        row = [None] * 63
        row[0], row[1] = "600664", "哈药股份"
        row[5], row[6], row[7] = 9.49, 2.15, 1_250_000_000
        row[13], row[21], row[25] = 83_000_000, 1.22, 8.65
        row[37], row[38], row[53], row[61] = 25_000_000_000, 20_000_000_000, 2.4, 18.6
        parsed = parse_industry_stock_row(row, date(2026, 9, 1), "881155")
        self.assertEqual(parsed["symbol"], "600664.SH")
        self.assertEqual(parsed["main_net"], 83_000_000)
        self.assertEqual(parsed["flow_convention"], "longhuvip_zs_stocklist_main_net_field13")
        self.assertEqual(parsed["pe"], 18.6)
        self.assertEqual(parsed["pb"], 2.4)

    def test_tencent_batch_parser_keeps_exchange_date_and_ohlc(self):
        fields = [""] * 39
        fields[1], fields[3], fields[4], fields[5] = "哈药股份", "9.49", "9.29", "9.30"
        fields[6], fields[30], fields[32] = "123456", "20260901150003", "2.15"
        fields[33], fields[34], fields[37] = "9.58", "9.18", "125000.5"
        text = f'v_sh600664="{"~".join(fields)}";'
        rows = parse_tencent_quote_text(text, {"sh600664": "600664.SH"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_date"], "20260901")
        self.assertEqual(rows[0]["high"], 9.58)
        self.assertEqual(rows[0]["low"], 9.18)
        self.assertEqual(rows[0]["vol"], 123456)
        self.assertEqual(rows[0]["amount"], 1_250_005_000)

    def test_symbol_normalization_is_explicit(self):
        self.assertEqual(normalize_stock_symbol("600664"), "600664.SH")
        self.assertEqual(normalize_stock_symbol("002212"), "002212.SZ")
        self.assertEqual(normalize_stock_symbol("920895"), "920895.BJ")
        self.assertIsNone(normalize_stock_symbol("399001"))

    def test_stock_snapshot_keeps_vendor_exchange_timestamp(self):
        parsed = parse_stock_snapshot_payload({
            "code": "600664", "name": "哈药股份", "day": "20260901", "preclose_px": 9.29,
            "real": {
                "last_px": 9.49, "open_px": 9.30, "high_px": 9.58, "low_px": 9.18,
                "time": "145901000", "px_change_rate": 2.15, "total_amount": 123456,
                "total_turnover": 125000000, "turnover_ratio": 8.65, "vol_ratio": 1.22,
            },
        }, "600664.SH")
        self.assertEqual(parsed["ts_code"], "600664.SH")
        self.assertEqual(parsed["trade_time"], "20260901145901")
        self.assertEqual(parsed["price"], 9.49)

    def test_stock_minutes_are_normalized_for_existing_feature_engine(self):
        rows = parse_stock_minute_payload({
            "trend": [
                ["09:30", 10.0, 10.0, 100],
                ["09:31", 10.1, 10.05, 60],
                ["13:00", 10.2, 10.08, 80],
            ],
        }, "600664.SH")
        self.assertEqual([row["volume_lot"] for row in rows], [100.0, 60.0, 80.0])
        self.assertEqual(rows[1]["amount"], 60300.0)
        self.assertEqual(rows[2]["cumulative_segment"], 1)
        self.assertFalse(rows[-1]["is_complete"])

    def test_shared_gateway_enforces_logical_cap_and_preserves_status(self):
        source = SharedLonghuReadSource("http://owner.test", "read-key")

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "rows": [{"ts_code": "600664.SH", "price": 12.3}],
                    "source_status": {"status": "completed", "source": "longhuvip:GetStockPanKou"},
                }

        calls = []

        def get(url, *, params, timeout):
            calls.append((url, params))
            self.assertEqual(timeout, 30.0)
            return Response()

        source._session.get = get
        rows, status = source.watch_quotes(["600664.SH", "600487.SH"], max_symbols=1)
        self.assertEqual(rows, [{"ts_code": "600664.SH", "price": 12.3}])
        self.assertEqual(calls[0][1]["symbols"], "600664.SH")
        self.assertTrue(status["truncated"])
        self.assertEqual(status["transport"], "shared_gateway")

    def test_shared_gateway_forwards_full_stock_api_contract(self):
        source = SharedLonghuReadSource("http://owner.test", "read-key")

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"target": "longhu_history", "calls": 3, "pages": []}

        calls = []

        def post(url, *, json, timeout):
            calls.append((url, json, timeout))
            return Response()

        source._session.post = post
        result = source.raw_call({
            "target": "longhu_history",
            "params": {"a": "GGList_JGCC", "c": "ZhuLiChiCang", "st": 650},
        })
        self.assertEqual(result["calls"], 3)
        self.assertEqual(calls[0][0], "http://owner.test/licensed/stock-api/call")
        self.assertEqual(calls[0][1]["params"]["st"], 650)
        self.assertGreaterEqual(calls[0][2], 180.0)

    def test_plate_list_paginates_larger_logical_reads_in_300_row_batches(self):
        source = LonghuVendorSource(LonghuVendorConfig(token="t", user_id="u", device_id="d"))
        offsets = []

        def vendor_row(code):
            row = [None] * 63
            row[0], row[1], row[5], row[13] = code, code, 10.0, 1_000.0
            return row

        def request(_url, params):
            self.assertLessEqual(params["st"], MAX_PAGE_SIZE)
            offsets.append(params["Index"])
            start = params["Index"]
            size = 300 if start == 0 else 5
            return {
                "Count": 305,
                "list": [vendor_row(f"{600000 + start + index:06d}") for index in range(size)],
            }

        source._json = request
        rows = source.plate_day("881001", date(2026, 8, 31), live=False)
        self.assertEqual(offsets, [0, 300])
        self.assertEqual(len(rows), 305)


if __name__ == "__main__":
    unittest.main()
