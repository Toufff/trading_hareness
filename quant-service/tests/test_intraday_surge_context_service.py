from __future__ import annotations

import asyncio
import unittest

from app.intraday_surge_context_service import capture


class IntradaySurgeContextServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_circuit_open_returns_explicit_watch_basket_without_fetching(self):
        called = False

        async def fetch(_symbol: str):
            nonlocal called
            called = True
            return []

        async def open_capabilities(_provider, _capabilities):
            return {"tencent_intraday_minute"}

        async def run_database(*_args, **_kwargs):
            raise AssertionError("circuit-open path must not persist a failed provider call")

        features, evidence = await capture(
            [{"symbol": "000001.SZ", "metadata": {"surge_strategy": {"enabled": True, "peer_symbols": ["000002.SZ"]}}}],
            mapped_peers={"000001.SZ": {"peer_symbols": ["000003.SZ"]}}, cache={}, max_symbols=lambda: 20,
            open_capabilities=open_capabilities, capability="tencent_intraday_minute", fetch_minutes=fetch,
            minute_features=lambda rows, **_kwargs: {"rows": rows}, persist_health=lambda *_args: None,
            run_database=run_database, safe_error=lambda value, _limit: value,
            handled_errors=(asyncio.TimeoutError, ValueError),
        )
        self.assertFalse(called)
        self.assertEqual(features, {})
        self.assertEqual(evidence["requested"], ["000001.SZ", "000002.SZ", "000003.SZ"])
        self.assertEqual(evidence["provider_status"], "circuit_open")

    async def test_cache_prevents_second_provider_call(self):
        calls: list[str] = []
        cache: dict[str, tuple[float, dict[str, object] | None, str | None]] = {}

        async def fetch(symbol: str):
            calls.append(symbol)
            return [{"time": "10:00", "close": 1}]

        async def open_capabilities(_provider, _capabilities):
            return set()

        async def run_database(action, *args, **_kwargs):
            return action(*args)

        kwargs = dict(
            mapped_peers=None, cache=cache, max_symbols=lambda: 20,
            open_capabilities=open_capabilities, capability="tencent_intraday_minute", fetch_minutes=fetch,
            minute_features=lambda rows, **_kwargs: {"latest": rows[-1]}, persist_health=lambda *_args: None,
            run_database=run_database, safe_error=lambda value, _limit: value,
            handled_errors=(asyncio.TimeoutError, ValueError),
        )
        watches = [{"symbol": "000001.SZ", "metadata": {}}]
        first, first_evidence = await capture(watches, **kwargs)
        second, second_evidence = await capture(watches, **kwargs)
        self.assertEqual(calls, ["000001.SZ"])
        self.assertEqual(first, second)
        self.assertEqual(first_evidence["provider_status"], "completed")
        self.assertEqual(second_evidence["provider_status"], "cached")


if __name__ == "__main__":
    unittest.main()
