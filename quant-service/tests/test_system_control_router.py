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
    def _client(self, *, health_payload=lambda: {"status": "ok"}) -> TestClient:
        app = FastAPI()
        app.include_router(build_system_control_router(SystemControlDependencies(
            health_payload=health_payload,
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
        self.assertEqual(calls[0][1], {"timeout_seconds": 3})
