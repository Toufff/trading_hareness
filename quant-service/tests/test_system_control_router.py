from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import Response

from app.routers.system_control import SystemControlDependencies, build_system_control_router


class _Unavailable(RuntimeError):
    pass


class SystemControlRouterTests(unittest.TestCase):
    def _client(self, *, health_payload=lambda: {"status": "ok"}, bootstrap=lambda: {"status": "ok"}) -> TestClient:
        app = FastAPI()
        app.include_router(build_system_control_router(SystemControlDependencies(
            health_payload=health_payload,
            database_unavailable_error=_Unavailable,
            metrics_response=lambda: Response(b"quant_test_metric 1\n", media_type="text/plain"),
            legacy_bootstrap=bootstrap,
        )))
        return TestClient(app)

    def test_operational_routes_preserve_urls_and_local_response_contracts(self) -> None:
        with self._client(bootstrap=lambda: {"status": "ok", "catalog": {"apis": 1}}) as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            metrics = client.get("/metrics")
            self.assertEqual(metrics.status_code, 200)
            self.assertIn("quant_test_metric 1", metrics.text)
            self.assertEqual(client.post("/api/v1/bootstrap").json(), {"status": "ok", "catalog": {"apis": 1}})

    def test_database_unavailable_is_a_strict_health_failure(self) -> None:
        def unavailable() -> dict[str, object]:
            raise _Unavailable("pool closed")

        with self._client(health_payload=unavailable) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "database unavailable: pool closed")
