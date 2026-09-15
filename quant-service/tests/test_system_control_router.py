from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import Response

from app.routers.system_control import SystemControlDependencies, build_system_control_router


class _Unavailable(RuntimeError):
    pass


class SystemControlRouterTests(unittest.TestCase):
    def _client(self, *, health_payload=lambda: {"status": "ok"}, async_probe=None) -> TestClient:
        app = FastAPI()
        app.include_router(build_system_control_router(SystemControlDependencies(
            health_payload=health_payload,
            async_database_probe=async_probe,
            database_unavailable_error=_Unavailable,
            metrics_response=lambda: Response(b"quant_test_metric 1\n", media_type="text/plain"),
        )))
        return TestClient(app)

    def test_operational_routes_preserve_urls_and_local_response_contracts(self) -> None:
        with self._client() as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            metrics = client.get("/metrics")
            self.assertEqual(metrics.status_code, 200)
            self.assertIn("quant_test_metric 1", metrics.text)

    def test_legacy_bootstrap_route_is_removed(self) -> None:
        with self._client() as client:
            response = client.post("/api/v1/bootstrap")
        self.assertEqual(response.status_code, 404)

    def test_database_unavailable_is_a_strict_health_failure_without_echoing_the_driver_error(self) -> None:
        def unavailable() -> dict[str, object]:
            raise _Unavailable("pool closed at host=db.internal port=5432 user=quant_app")

        with self._client(health_payload=unavailable) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "database unavailable")
        self.assertNotIn("host=", response.text)
        self.assertNotIn("pool closed", response.text)

    def test_sync_healthy_async_broken_is_503(self):
        async def broken():
            raise RuntimeError('secret connection details')
        with self._client(async_probe=broken) as client:
            response = client.get('/health')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail'], 'async database unavailable')
        self.assertNotIn('secret', response.text)

    def test_async_probe_recovers_without_recreating_app(self):
        attempts = []
        async def probe():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('temporary outage')
        with self._client(async_probe=probe) as client:
            self.assertEqual(client.get('/health').status_code, 503)
            self.assertEqual(client.get('/health').status_code, 200)

    def test_async_probe_timeout_is_bounded(self):
        async def stuck():
            await asyncio.sleep(60)
        async def expire(awaitable, *, timeout):
            awaitable.close()
            raise TimeoutError
        with self._client(async_probe=stuck) as client:
            with patch("app.routers.system_control.asyncio.wait_for", new=expire):
                self.assertEqual(client.get('/health').status_code, 503)

    def test_async_probe_allows_tunnel_latency_budget(self) -> None:
        observed: list[float] = []

        async def probe() -> None:
            return None

        async def capture(awaitable, *, timeout):
            observed.append(timeout)
            return await awaitable

        with patch("app.routers.system_control.asyncio.wait_for", new=capture):
            with self._client(async_probe=probe) as client:
                self.assertEqual(client.get("/health").status_code, 200)

        self.assertEqual(observed, [10.0, 15])

    def test_sync_health_timeout_is_reported_as_degraded_not_internal_error(self) -> None:
        async def timed_out(*_args, **_kwargs):
            raise TimeoutError("executor saturated")

        with patch("app.routers.system_control.run_database_blocking", new=timed_out):
            with self._client() as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "health probe timeout"})
        self.assertNotIn("executor saturated", response.text)

    def test_health_runs_through_the_bounded_fast_lane(self) -> None:
        """WP6: /health must not run the DB probe in anyio's unbounded threadpool."""
        calls: list[tuple[object, dict[str, object]]] = []

        async def bounded(action, *args, **kwargs):
            calls.append((action, kwargs))
            return action(*args)

        with patch("app.routers.system_control.run_database_blocking", new=bounded):
            router = build_system_control_router(SystemControlDependencies(
                health_payload=lambda: {"status": "ok"},
                database_unavailable_error=_Unavailable,
                metrics_response=lambda: Response(b"", media_type="text/plain"),
            ))
            endpoint = next(route.endpoint for route in router.routes if route.path == "/health")
            payload = asyncio.run(endpoint())

        self.assertEqual(payload, {"status": "ok"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {"timeout_seconds": 15})
