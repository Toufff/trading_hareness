from __future__ import annotations

import unittest
from datetime import date

from app.longhu_vendor_source import (
    MAX_PAGE_SIZE,
    LonghuVendorConfig,
    normalize_stock_symbol,
    parse_industry_stock_row,
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


if __name__ == "__main__":
    unittest.main()
