from __future__ import annotations

import unittest

from app.limit_pool_merge import merge_limit_pool_sources


class LimitPoolMergeTests(unittest.TestCase):
    def test_merge_uses_symbol_identity_and_only_enriches_missing_primary_fields(self) -> None:
        result = merge_limit_pool_sources(
            [{"row_data": {"ts_code": "000001.SZ", "name": "主源名", "price": 10.0}, "provider_key": "tushare_super_sdk"},
             {"row_data": {"ts_code": "not-a-symbol"}}],
            [{"symbol": "000001.SZ", "source": "akshare", "body": {
                "名称": "不应覆盖", "最新价": 11.0, "成交额": 123.0, "连板数": 2,
            }},
             {"symbol": "600000.SH", "source": "akshare", "body": {"名称": "独立标的", "连板数": 1}}],
            json_safe=lambda value: value,
            number=lambda value: float(value) if value not in (None, "") else None,
        )
        by_symbol = {item["ts_code"]: item for item in result["items"]}
        self.assertEqual(set(by_symbol), {"000001.SZ", "600000.SH"})
        self.assertEqual(by_symbol["000001.SZ"]["name"], "主源名")
        self.assertEqual(by_symbol["000001.SZ"]["price"], 10.0)
        self.assertEqual(by_symbol["000001.SZ"]["amount"], 123.0)
        self.assertEqual(by_symbol["000001.SZ"]["sources"], ["tushare_limit_list_ths", "eastmoney_stock_zt_pool_em"])
        self.assertEqual(by_symbol["600000.SH"]["tag"], "首板")
        self.assertEqual(result["coverage"]["status"], "two_source_union")
        self.assertEqual(result["coverage"]["union_count"], 2)

    def test_invalid_json_body_is_local_empty_evidence_not_a_failure(self) -> None:
        result = merge_limit_pool_sources(
            [], [{"symbol": "000001.SZ", "body": "not-json"}],
            json_safe=lambda value: value,
            number=lambda value: float(value) if value not in (None, "") else None,
        )
        self.assertEqual(result["coverage"]["status"], "single_source_only")
        self.assertEqual(result["items"][0]["ts_code"], "000001.SZ")
        self.assertEqual(result["items"][0]["tag"], "首板")


if __name__ == "__main__":
    unittest.main()
