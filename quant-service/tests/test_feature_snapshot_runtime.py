"""Transaction and dependency-boundary coverage for feature snapshots."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock
import json
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


class AnalystEvidenceIsJsonSerializableTests(unittest.TestCase):
    """Evidence rows must not carry the driver's own numeric type.

    ``direction`` and ``strength`` are numeric columns. The consensus above
    coerces them; the evidence list used to pass them through raw, so psycopg
    received a Decimal and the whole feature snapshot failed to persist for
    every symbol that had an eligible observation.
    """

    def _feature(self, rows):
        from app.feature_read_repository import analyst_feature
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = rows
        return analyst_feature(connection, "000001.SZ", date(2026, 8, 27), lambda v: float(v or 0))

    def _row(self, **overrides):
        row = {"remote_analyst_id": "a1", "direction": Decimal("1"), "strength": Decimal("0.78"),
               "extraction_confidence": Decimal("0.9"), "horizon_days": 20, "evidence": "note"}
        row.update(overrides)
        return row

    def test_evidence_survives_a_plain_json_dump(self):
        feature = self._feature([self._row()])
        json.dumps(feature)  # would raise on a Decimal

    def test_the_coerced_values_keep_their_magnitude(self):
        feature = self._feature([self._row()])
        self.assertEqual(feature["evidence"][0]["strength"], 0.78)
        self.assertEqual(feature["evidence"][0]["direction"], 1.0)

    def test_an_empty_observation_set_still_serializes(self):
        json.dumps(self._feature([]))


class StoredSnapshotMatchesItsKeyEncodingTests(unittest.TestCase):
    """The stored payload must accept whatever the snapshot key hashed.

    The key is computed with ``default=str``; the stored copy used psycopg's
    default encoder, which has none. A payload containing any driver-native
    type therefore produced a valid key and then failed to persist the row it
    identified.
    """

    def _encode(self, payload):
        from app.stable_json import stable_json
        return stable_json(payload).dumps(payload)

    def test_a_decimal_is_storable(self):
        self.assertIn("0.78", self._encode({"strength": Decimal("0.78")}))

    def test_a_datetime_is_storable(self):
        from datetime import datetime
        self.assertIn("2026-08-27", self._encode({"seen": datetime(2026, 8, 27, 9, 30)}))

    def test_it_matches_the_key_encoding_byte_for_byte(self):
        payload = {"b": Decimal("1.5"), "a": 2}
        expected = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              default=str, separators=(",", ":"))
        self.assertEqual(self._encode(payload), expected)
