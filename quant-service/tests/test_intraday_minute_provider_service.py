from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import unittest
from typing import Any

from app.intraday_minute_provider_service import (
    fetch_bounded_minute_context, filter_fresh_minute_rows, normalize_super_get_minute_rows,
)
from app.numeric_utils import intraday_number


class IntradayMinuteProviderServiceTests(unittest.TestCase):
    def test_freshness_gate_accepts_recent_city_row_and_rejects_stale_promax_row(self) -> None:
        observed_at = datetime(2026, 9, 1, 15, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        fresh, stale = filter_fresh_minute_rows([
            {"updated_at": "2026-09-01T14:59:20.000", "vol": 1},
            {"trade_time": "2026-09-01 11:30:00.000", "vol": 1},
            {"vol": 1},
        ], observed_at=observed_at, max_age_seconds=90)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["updated_at"], "2026-09-01T14:59:20.000")
        self.assertEqual(stale, 2)

    def test_normalization_orders_rows_and_excludes_invalid_values(self) -> None:
        rows = normalize_super_get_minute_rows([
            {"time": "2026-08-16 10:02:00", "vol": "10", "amount": "10200", "close": "10.2"},
            {"time": "2026-08-16 10:00:00", "vol": "5", "amount": "5000", "close": "10"},
            {"time": "2026-08-16 10:01:00", "vol": "-1", "amount": "10", "close": "10"},
            {"time": "2026-08-16 10:03:00", "vol": "1", "amount": "0", "close": "0"},
        ], number=intraday_number)
        self.assertEqual([item["time"] for item in rows], ["2026-08-16 10:00:00", "2026-08-16 10:02:00"])
        self.assertEqual(rows[0]["volume_lot"], 5.0)
        self.assertAlmostEqual(rows[0]["vwap"], 1000.0)
        self.assertAlmostEqual(rows[1]["vwap"], 1013.3333333333, places=8)
        self.assertTrue(all(item["is_complete"] for item in rows))

    def test_collection_isolated_per_symbol_and_preserves_empty_source_status(self) -> None:
        async def fetch_rows(symbol: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            if symbol == "000003.SZ":
                raise RuntimeError("isolated upstream failure")
            if symbol == "000002.SZ":
                return {"status": "empty", "source": "tushare_rt_min"}, []
            rows = [
                {"time": f"2026-08-16 10:{index:02d}:00", "vol": 1, "amount": 1000 + index, "close": 10 + index / 100}
                for index in range(6)
            ]
            return {"status": "completed", "source": "tushare_rt_min"}, rows

        calls: list[tuple[int, str]] = []

        def feature_builder(rows: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
            calls.append((len(rows), source))
            return {"count": len(rows), "source": source}

        result = asyncio.run(fetch_bounded_minute_context(
            ["000001.SZ", "000002.SZ", "000003.SZ"], fetch_rows=fetch_rows,
            feature_builder=feature_builder, number=intraday_number,
        ))
        self.assertEqual(sorted(result), ["000001.SZ", "000002.SZ"])
        self.assertEqual(result["000001.SZ"]["feature"], {"count": 6, "source": "tushare_super_get_rt_min"})
        self.assertEqual(result["000001.SZ"]["latest"]["time"], "2026-08-16 10:05:00")
        self.assertEqual(result["000002.SZ"]["source"]["status"], "empty")
        self.assertNotIn("latest", result["000002.SZ"])
        self.assertEqual(calls, [(6, "tushare_super_get_rt_min")])

    def test_live_collection_marks_all_stale_rows_without_building_features(self) -> None:
        async def fetch_rows(_symbol: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            return {"status": "completed", "source": "promax"}, [{
                "trade_time": "2026-09-01 11:30:00.000", "vol": 1, "amount": 100, "close": 10,
            }]

        calls: list[int] = []

        def feature_builder(rows: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
            calls.append(len(rows))
            return {"count": len(rows)}

        result = asyncio.run(fetch_bounded_minute_context(
            ["000001.SZ"], fetch_rows=fetch_rows, feature_builder=feature_builder,
            number=intraday_number,
            observed_at=datetime(2026, 9, 1, 7, 0, tzinfo=ZoneInfo("UTC")), max_age_seconds=90,
        ))
        self.assertEqual(result["000001.SZ"]["source"]["status"], "stale")
        self.assertEqual(result["000001.SZ"]["source"]["fresh_rows"], 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
