"""Pure contracts that keep provider semantics and runtime ownership explicit."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.platform.evidence_contracts import (
    evidence_contract_catalog,
    materialize_evidence_status,
)
from app.platform.runtime_task_registry import (
    runtime_profile_owns_task,
    runtime_task_contract_catalog,
)
from app.platform.strategy_registry import strategy_contract_catalog
from app.health_read_model import HealthDependencies, health_payload


class PlatformContractRegistryTests(unittest.TestCase):
    def test_bounded_eastmoney_flow_never_claims_cross_sectional_decision_eligibility(self) -> None:
        status = materialize_evidence_status(
            "eastmoney_watch_flow", {"status": "fresh", "age_seconds": 0.0}, matched_symbols=3,
        )
        self.assertEqual(status["provider_key"], "eastmoney_free")
        self.assertEqual(status["scope"], "explicit_watchlist_only")
        self.assertFalse(status["cross_sectional"])
        self.assertFalse(status["decision_eligible"])
        self.assertEqual(status["matched_symbols"], 3)

    def test_evidence_catalog_is_secret_free_and_deterministic(self) -> None:
        catalog = evidence_contract_catalog()
        self.assertEqual([item["key"] for item in catalog], sorted(item["key"] for item in catalog))
        self.assertIn("fuyao_all_a_snapshot", {item["key"] for item in catalog})
        self.assertNotIn("API_KEY", str(catalog))

    def test_runtime_profile_ownership_is_registry_driven(self) -> None:
        self.assertTrue(runtime_profile_owns_task("intraday_edge", "intraday_monitor"))
        self.assertFalse(runtime_profile_owns_task("research", "intraday_monitor"))
        self.assertTrue(runtime_profile_owns_task("research", "strategy_review"))
        self.assertTrue(runtime_profile_owns_task("full", "strategy_review"))
        catalog = runtime_task_contract_catalog()
        monitor = next(item for item in catalog if item["label"] == "intraday_monitor")
        self.assertEqual(monitor["owner_profile"], "intraday_edge")
        self.assertIn("intraday_scan_runs", monitor["evidence_datasets"])

    def test_strategy_catalog_declares_replay_inputs_and_never_live_effect(self) -> None:
        catalog = strategy_contract_catalog()
        intraday = next(item for item in catalog if item["key"] == "intraday_watchlist_confirmation")
        self.assertEqual(intraday["input_contract"], "intraday-rule-input-v2")
        self.assertEqual({item["live_effect"] for item in catalog}, {"none"})

    def test_health_can_publish_declared_task_contracts_without_provider_io(self) -> None:
        class Result:
            def fetchone(self): return {"count": 0, "expires_at": None, "updated_at": None}
            def fetchall(self): return []

        class Database:
            def ping(self): return None
            def pool_status(self): return {"pool_size": 1, "available": 1, "waiting": 0}
            def transaction(self):
                class Transaction:
                    def __enter__(self): return self
                    def __exit__(self, *_): return False
                    def execute(self, *_args, **_kwargs): return Result()
                return Transaction()

        payload = health_payload(HealthDependencies(
            database=Database(), post_close_lease_key="post-close", background_loop_lease_seconds=lambda: 30,
            data_directory=lambda: Path("/tmp"), resource_status=lambda _: {}, public_http_client_status=lambda: {},
            alert_http_client_status=lambda: {}, provider_http_client_status=lambda: {},
            remote_archive_http_client_status=lambda: {}, network_status=lambda: {},
            provider_request_reservation_status=lambda: {}, runtime_executor_status=lambda: {},
            super_get_executor_status=lambda: {}, provider_status=lambda: [], free_provider_status=lambda: [],
            realtime_market_session=lambda: (False, "closed"), board_curve_session=lambda: (False, "closed"),
            scan_interval_seconds=lambda: 30, effective_scan_interval_seconds=lambda *_: 30,
            high_frequency_window=lambda _: False, super_get_fast_interval_seconds=lambda: 1.0,
            super_get_fast_max_in_flight=lambda: 1, fast_quote_retention_days=lambda: 7,
            board_curve_enabled=lambda: True, board_curve_retention_days=lambda: 60,
            board_rotation_retention_days=lambda: 60, set_db_pool_gauge=lambda _: None,
            set_open_circuit_gauge=lambda _: None, runtime_task_contracts=runtime_task_contract_catalog,
        ))
        self.assertTrue(any(item["label"] == "intraday_monitor" for item in payload["runtime_task_contracts"]))


if __name__ == "__main__":
    unittest.main()
