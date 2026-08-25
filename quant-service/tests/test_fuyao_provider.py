from __future__ import annotations

import unittest

from app.fuyao_provider import FuyaoProviderError, normalize_snapshot_rows, validate_capability_query


class FuyaoProviderTests(unittest.TestCase):
    def test_normalizes_official_snapshot_without_inventing_flow_fields(self) -> None:
        rows = normalize_snapshot_rows({
            "timestamp": 1787625155000,
            "item": [{
                "thscode": "600519.SH", "last_price": 1305.99,
                "prev_price": 1304.66, "price_change_ratio_pct": 0.101942,
                "volume": 1006124, "turnover": 1316263770,
            }],
        })
        self.assertEqual(rows[0]["symbol"], "600519.SH")
        self.assertEqual(rows[0]["price_source"], "fuyao_ths_all_a_snapshot")
        self.assertEqual(rows[0]["turnover"], 1316263770.0)
        self.assertNotIn("main_net_inflow", rows[0])

    def test_capability_query_is_allowlisted_and_scalar(self) -> None:
        self.assertEqual(
            validate_capability_query("a_share_prices_snapshot", {"thscodes": "600519.SH", "limit": 100}),
            "/api/a-share/prices/snapshot",
        )
        with self.assertRaises(FuyaoProviderError):
            validate_capability_query("not_a_capability", {})
        with self.assertRaises(FuyaoProviderError):
            validate_capability_query("a_share_prices_snapshot", {"nested": {"not": "allowed"}})


if __name__ == "__main__":
    unittest.main()
