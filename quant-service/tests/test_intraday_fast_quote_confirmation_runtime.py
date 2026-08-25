from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import unittest

from app.intraday_fast_quote_confirmation_runtime import latest_confirmations


class FastQuoteConfirmationRuntimeTests(unittest.TestCase):
    def test_empty_symbols_do_not_read_storage(self):
        async def read_latest(_symbols):
            raise AssertionError("must not read")

        result = asyncio.run(latest_confirmations([], {}, datetime.now(timezone.utc), read_latest=read_latest, confirm=lambda *_: {}))
        self.assertEqual(result, {})

    def test_maps_persisted_rows_by_symbol_before_confirmation(self):
        async def read_latest(symbols):
            self.assertEqual(symbols, ["000001.SZ"])
            return [{"symbol": "000001.SZ", "price": 10}]

        result = asyncio.run(latest_confirmations(
            ["000001.SZ"], {"000001.SZ": {"price": 11}}, datetime.now(timezone.utc),
            read_latest=read_latest, confirm=lambda quote, fast, _: {"quote": quote["price"], "fast": fast["price"]},
        ))
        self.assertEqual(result, {"000001.SZ": {"quote": 11, "fast": 10}})


if __name__ == "__main__":
    unittest.main()
