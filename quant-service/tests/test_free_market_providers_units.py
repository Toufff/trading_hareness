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


if __name__ == "__main__":
    unittest.main()
