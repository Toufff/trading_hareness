"""Transaction and dependency-boundary coverage for feature snapshots."""

from __future__ import annotations

from datetime import date
import unittest

from app.feature_snapshot_runtime import FeatureSnapshotRuntime, FeatureSnapshotRuntimeDependencies


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args):
        return None


class FeatureSnapshotRuntimeTests(unittest.TestCase):
    def test_materializes_through_one_transaction_with_explicit_contract_ports(self):
        connection = object()
        database = type("Database", (), {"transaction": lambda _self: _Transaction(connection)})()
        received = {}

        def materialize(received_connection, as_of_date, universe_key, **kwargs):
            received.update(connection=received_connection, as_of_date=as_of_date, universe_key=universe_key, **kwargs)
            return {"status": "completed", "snapshot_key": "snapshot-1"}

        runtime = FeatureSnapshotRuntime(FeatureSnapshotRuntimeDependencies(
            database=database,
            materialize=materialize,
            feature_version="features-v1",
            number=float,
            market_regime=lambda *_: "neutral",
            analyst_text_factor_summary=lambda *_: {"market": {}},
            latest_tushare_row=lambda *_: None,
            analyst_feature=lambda *_: {},
        ))

        result = runtime.build(date(2026, 8, 24), "core")

        self.assertEqual(result["snapshot_key"], "snapshot-1")
        self.assertIs(received["connection"], connection)
        self.assertEqual(received["as_of_date"], date(2026, 8, 24))
        self.assertEqual(received["universe_key"], "core")
        self.assertEqual(received["feature_version"], "features-v1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
