"""Contract coverage for the scan persistence runtime adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest
import uuid

from app.intraday_scan_persistence_runtime import IntradayScanPersistenceRuntime


class IntradayScanPersistenceRuntimeTests(unittest.TestCase):
    def test_forwards_one_complete_scan_to_the_single_transaction_port(self) -> None:
        dependency_graph = object()
        received: dict[str, object] = {}

        def persist_transaction(dependencies, **kwargs):
            received["dependencies"] = dependencies
            received.update(kwargs)
            return [{"signal_event_id": "one"}]

        runtime = IntradayScanPersistenceRuntime(
            dependencies=dependency_graph,  # type: ignore[arg-type]
            persist_transaction=persist_transaction,
        )
        scan_id = uuid.uuid4()
        observed_at = datetime(2026, 8, 24, tzinfo=timezone.utc)
        result = runtime.persist(
            scan_id, observed_at, ["000001.SZ"], {"source": "fresh"}, [{"symbol": "000001.SZ"}],
            {"000001.SZ": {"price": 10.0}}, [{"symbol": "000001.SZ"}], 12,
            {"000001.SZ": {"minute": "ok"}}, {"000001.SZ": {"surge": "ok"}},
            {"000001.SZ": {"peers": []}}, {"000001.SZ": {"status": "confirmed"}},
        )

        self.assertEqual(result, [{"signal_event_id": "one"}])
        self.assertIs(received["dependencies"], dependency_graph)
        self.assertEqual(received["scan_id"], scan_id)
        self.assertEqual(received["quote_latency_ms"], 12)
        self.assertEqual(received["fast_confirmations"], {"000001.SZ": {"status": "confirmed"}})


if __name__ == "__main__":
    unittest.main()
