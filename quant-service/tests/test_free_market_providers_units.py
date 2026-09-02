from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import free_market_providers


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class EastmoneyDailyAmountUnitTests(unittest.TestCase):
    def test_amount_is_converted_from_yuan_to_thousand_yuan(self) -> None:
        # Eastmoney's f57 kline field is documented in yuan; the canonical
        # daily contract (matching Tushare) is thousand yuan.  1,250,000 yuan
        # of turnover must be stored as 1250, not 1250000.
        payload = {"data": {"klines": ["2026-08-10,10,10.5,11,9,100,1250000,,,2.5"]}}
        fake_response = _FakeResponse(payload)

        async def run() -> list[dict[str, object]]:
            with patch.object(free_market_providers, "_request_with_retry", new=AsyncMock(return_value=fake_response)):
                return await free_market_providers.eastmoney_daily("000001.SZ", "20260801", "20260810")

        rows = asyncio.run(run())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "1250")
        # Volume is already in the canonical lot unit and must pass through.
        self.assertEqual(rows[0]["vol"], "100")

    def test_missing_amount_is_none_not_a_conversion_error(self) -> None:
        payload = {"data": {"klines": ["2026-08-10,10,10.5,11,9,100,-,,,2.5"]}}
        fake_response = _FakeResponse(payload)

        async def run() -> list[dict[str, object]]:
            with patch.object(free_market_providers, "_request_with_retry", new=AsyncMock(return_value=fake_response)):
                return await free_market_providers.eastmoney_daily("000001.SZ", "20260801", "20260810")

        rows = asyncio.run(run())
        self.assertIsNone(rows[0]["amount"])


class TencentIntradayMinutesAmountScaleTests(unittest.TestCase):
    def test_zero_volume_opening_minute_does_not_lock_in_the_wrong_scale(self) -> None:
        # A zero-volume opening tick used to lock amount_scale at 1.0 for the
        # rest of the day even once trading began at the audited 100x
        # cumulative-amount convention; the scale must be determined at the
        # first row that actually has cumulative_volume_lot > 0.
        payload = {"data": {"sz000001": {"data": {
            "data": [
                "0930 293.0 0 0",          # pre-open: zero volume/amount
                "0931 293.0 1000 293000",  # first real minute: audited 100x convention
            ],
        }}}}
        fake_response = _FakeResponse(payload)

        async def run() -> list[dict[str, object]]:
            with patch.object(free_market_providers, "_request_with_retry", new=AsyncMock(return_value=fake_response)):
                return await free_market_providers.tencent_intraday_minutes("000001.SZ")

        rows = asyncio.run(run())
        self.assertIsNone(rows[0]["amount_unit_scale"])
        self.assertEqual(rows[1]["amount_unit_scale"], 100.0)
        # The scale is now correctly applied, not frozen at 1.0 from the
        # degenerate all-zero opening row.
        self.assertEqual(rows[1]["cumulative_amount"], 293000.0 * 100.0)


if __name__ == "__main__":
    unittest.main()
