"""Startup control-plane runtime coverage without provider transport."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from app.provider_control_plane_runtime import ProviderControlPlaneRuntime, ProviderControlPlaneRuntimeDependencies


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args):
        return None


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement, params))


class ProviderControlPlaneRuntimeTests(unittest.TestCase):
    def test_initialization_mirrors_limits_and_declares_expected_provider_matrix(self):
        connection = _Connection()
        database = type("Database", (), {"transaction": lambda _self: _Transaction(connection)})()
        config = SimpleNamespace(key="tushare_primary", rate_limit_per_minute=60)
        items = [
            {"api_name": "daily", "catalog_origin": "official", "permission_model": "points", "min_points": 1,
             "request_policy": "bounded", "model_role": "research", "priority": "high"},
            {"api_name": "stock_basic", "catalog_origin": "official", "permission_model": "points", "min_points": 1,
             "request_policy": "bounded", "model_role": "research", "priority": "high"},
        ]
        runtime = ProviderControlPlaneRuntime(ProviderControlPlaneRuntimeDependencies(
            database=database,
            provider_configs=lambda: {"primary": config},
            catalog_items=lambda: items,
            capability_contract=lambda _name: SimpleNamespace(frequency="60/min", decision_eligible=False, note="declared"),
            super_get_verified_apis=frozenset({"daily"}),
            json_value=lambda value: value,
        ))

        runtime.initialize()

        self.assertEqual(connection.calls[0][1], (60, "tushare_primary"))
        self.assertEqual(connection.calls[1][1], (60, "tushare_primary"))
        declarations = [call[1][:2] for call in connection.calls[2:]]
        self.assertEqual(declarations, [
            ("tushare_primary", "daily"), ("tushare_super_sdk", "daily"),
            ("tushare_super_get", "daily"), ("tushare_primary", "stock_basic"),
            ("tushare_super_sdk", "stock_basic"), ("tushare_backup", "stock_basic"),
        ])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
