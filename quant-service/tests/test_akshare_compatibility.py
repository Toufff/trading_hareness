"""Regression coverage for AKShare version-specific call contracts."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from app.akshare_provider import akshare_block_trade_supplements


class AkShareCompatibilityTests(unittest.TestCase):
    def test_block_trade_aggregate_calls_match_1_18_93_signatures(self) -> None:
        calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

        class FakeAkShare:
            def __getattr__(self, name: str):
                def invoke(*args: object, **kwargs: object) -> list[dict[str, object]]:
                    calls.append((name, args, kwargs))
                    return []
                return invoke

        def invoke_action(_name: str, action, attempts: int = 2):
            self.assertEqual(attempts, 2)
            return action(FakeAkShare())

        with patch("app.akshare_provider._retry_call", side_effect=invoke_action):
            self.assertEqual(akshare_block_trade_supplements(date(2026, 8, 20)), [])

        by_name = {name: (args, kwargs) for name, args, kwargs in calls}
        self.assertEqual(by_name["stock_dzjy_sctj"], ((), {}))
        self.assertEqual(by_name["stock_dzjy_yybph"], ((), {"symbol": "近一月"}))
        self.assertEqual(by_name["stock_dzjy_hygtj"], ((), {"symbol": "近一月"}))


if __name__ == "__main__":
    unittest.main()
