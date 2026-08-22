from __future__ import annotations

from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from app.provider_probe_service import audit_tushare_capabilities, probe_realtime


class ProviderProbeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_keeps_unsupported_routes_visible_without_fetching(self):
        fetched: list[object] = []

        async def session(_api_name: str):
            return True, "continuous auction"

        async def fetch(_label, request):
            fetched.append(request)
            return {"status": "completed"}, []

        result = await probe_realtime(
            SimpleNamespace(symbols=["000001.SZ"], frequency="1MIN", etf_symbol="159919.SZ",
                            index_symbol="000300.SH", sw_symbol="801080.SI", futures_symbol=None),
            realtime_probe_matrix=lambda **_kwargs: [("rt_k", {"ts_code": "000001.SZ"})],
            default_probe_params=lambda *_args, **_kwargs: None,
            realtime_market_session=session,
            provider_candidates=lambda _api, _provider: [],
            fetch=fetch,
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(len(result["results"]), 3)
        self.assertTrue(all(item["availability"] == "unsupported" for item in result["results"]))
        self.assertEqual(fetched, [])

    async def test_realtime_uses_daily_row_bound_and_preserves_result(self):
        requests = []

        async def session(_api_name: str):
            return True, "continuous auction"

        async def fetch(_label, request):
            requests.append(request)
            return {"status": "completed", "stored": 1}, []

        result = await probe_realtime(
            SimpleNamespace(symbols=["000001.SZ"], frequency="1MIN", etf_symbol="159919.SZ",
                            index_symbol="000300.SH", sw_symbol="801080.SI", futures_symbol=None),
            realtime_probe_matrix=lambda **_kwargs: [("rt_min_daily", {"ts_code": "000001.SZ", "freq": "1MIN"})],
            default_probe_params=lambda *_args, **_kwargs: None,
            realtime_market_session=session,
            provider_candidates=lambda _api, provider: [provider] if provider == "super_get" else [],
            fetch=fetch,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].max_rows, 260)
        self.assertEqual(result["results"][-1]["provider"], "super_get")

    async def test_capability_audit_keeps_local_capacity_out_of_provider_failure(self):
        calls = {"timeout": 0, "read": 0}

        async def fetch(_request):
            raise HTTPException(status_code=503, detail="local processing capacity is temporarily saturated; retry shortly")

        async def timeout(_provider, _api_name):
            calls["timeout"] += 1

        async def read(_provider, _api_name):
            calls["read"] += 1
            return None

        result = await audit_tushare_capabilities(
            SimpleNamespace(api_names=["daily"], providers=["super"], symbol="000001.SZ", as_of_date=None, max_rows=10),
            today=lambda: __import__("datetime").date(2026, 8, 21),
            api_capability=lambda _api: SimpleNamespace(status="declared"),
            default_probe_params=lambda *_args, **_kwargs: {"ts_code": "000001.SZ"},
            historical_minute_apis=frozenset(), realtime_market_hours_apis=frozenset(),
            realtime_market_session=lambda _api: None,
            fetch_catalog=fetch, record_timeout=timeout, load_observation=read,
            is_local_capacity_error=lambda error: error.status_code == 503,
            is_circuit_open_error=lambda _error: False,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["results"][0]["availability"], "local_capacity")
        self.assertEqual(calls, {"timeout": 0, "read": 0})

    async def test_capability_audit_records_timeout_and_returns_failed(self):
        timeouts: list[tuple[str, str]] = []

        async def never_finishes(_request):
            await __import__("asyncio").sleep(10)
            return {"status": "completed"}

        async def record_timeout(provider, api_name):
            timeouts.append((provider, api_name))

        async def load(_provider, _api_name):
            return None

        result = await audit_tushare_capabilities(
            SimpleNamespace(api_names=["daily"], providers=["super"], symbol="000001.SZ", as_of_date=None, max_rows=10),
            today=lambda: __import__("datetime").date(2026, 8, 21),
            api_capability=lambda _api: SimpleNamespace(status="declared"),
            default_probe_params=lambda *_args, **_kwargs: {"ts_code": "000001.SZ"},
            historical_minute_apis=frozenset(), realtime_market_hours_apis=frozenset(),
            realtime_market_session=lambda _api: None,
            fetch_catalog=never_finishes, record_timeout=record_timeout, load_observation=load,
            is_local_capacity_error=lambda _error: False, is_circuit_open_error=lambda _error: False,
            timeout_seconds=0.001,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["reason"], "audit_timeout_25s")
        self.assertEqual(timeouts, [("super", "daily")])


if __name__ == "__main__":
    unittest.main()
