from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
import uuid

from app.intraday_scan_signal_persistence import (
    IntradayScanPersistenceServiceDependencies,
    IntradayScanSignalPersistenceDependencies,
    persist_scan_signals,
    persist_scan_transaction,
)


class _Connection:
    def __init__(self):
        self.executed = []

    def execute(self, query, params):
        self.executed.append((query, params))


class _Database:
    def __init__(self, connection):
        self.connection = connection
        self.entered = 0
        self.exited = 0

    def transaction(self):
        database = self

        class _Transaction:
            def __enter__(self):
                database.entered += 1
                return database.connection

            def __exit__(self, *args):
                database.exited += 1

        return _Transaction()


class IntradayScanSignalPersistenceTests(unittest.TestCase):
    def test_live_adapter_preserves_one_transaction_for_entire_scan(self):
        connection = _Connection()
        database = _Database(connection)
        observed_at = datetime(2026, 8, 22, 2, tzinfo=timezone.utc)
        prepared = SimpleNamespace(
            previous_by_symbol={}, daily_factors_by_symbol={}, raw_minute_features_by_symbol={},
            minute_volume_profiles_by_symbol={}, order_book_by_symbol={}, market_contexts={},
            paper_positions={}, snapshot_payload={}, candidate_sector_keys={}, shadow_priors={},
            rebound_priors={}, first_eac_by_symbol={}, probability_profiles={}, session_start=observed_at,
        )
        signal_dependencies = IntradayScanSignalPersistenceDependencies(
            prepare_inputs=lambda *_args, **_kwargs: prepared,
            preparation_dependencies=object(), quote_source=lambda _: "tencent_watch_batch",
            json_safe=lambda value: value, persist_rule_input_snapshot=lambda *_args, **_kwargs: None,
            attach_volume_time_profile=lambda *_args, **_kwargs: None, number=lambda value: value,
            aggregate_order_book_observations=lambda *_args, **_kwargs: None,
            generate_signals=lambda **_kwargs: [], signal_generation_dependencies=object(),
            load_event_state=lambda *_args, **_kwargs: {},
            persist_generated_signals=lambda *_args, **_kwargs: [], signal_event_persistence_dependencies=object(),
        )
        result = persist_scan_transaction(
            IntradayScanPersistenceServiceDependencies(
                database=database, signal_dependencies=signal_dependencies,
                confirmation_window=300, signal_model_version="v1", factor_contract_version="v2",
            ),
            scan_id=uuid.uuid4(), observed_at=observed_at, selected_symbols=["000001.SZ"],
            source_status={}, watches=[{"symbol": "000001.SZ"}], quotes={}, tencent_rows=[],
            quote_latency_ms=0, tushare_minutes={}, surge_features={}, peer_contexts={}, fast_confirmations={},
        )
        self.assertEqual(result, [])
        self.assertEqual((database.entered, database.exited), (1, 1))

    def test_freezes_then_generates_and_persists_inside_caller_transaction(self):
        observed_at = datetime(2026, 8, 22, 2, tzinfo=timezone.utc)
        connection = _Connection()
        calls = []
        prepared = SimpleNamespace(
            previous_by_symbol={"000001.SZ": {"price": 9.8}},
            daily_factors_by_symbol={"000001.SZ": {"status": "ready"}},
            raw_minute_features_by_symbol={"000001.SZ": {"volume": 1}},
            minute_volume_profiles_by_symbol={"000001.SZ": {"profile": 2}},
            order_book_by_symbol={"000001.SZ": [{"raw": {}}]},
            market_contexts={(observed_at, "000001.SZ"): {"regime": "neutral"}},
            paper_positions={"000001.SZ": {"quantity": 100}},
            snapshot_payload={"drawdown": 0.01},
            candidate_sector_keys={"000001.SZ": ["sector-a"]},
            shadow_priors={"000001.SZ": {"prior": 0.2}},
            rebound_priors={"000001.SZ": {"prior": 0.3}},
            first_eac_by_symbol={"000001.SZ": {"observed_at": observed_at}},
            probability_profiles={"watch": {"sample_size": 0}},
            session_start=observed_at,
        )

        def prepare_inputs(*args, **kwargs):
            self.assertIs(args[0], connection)
            self.assertEqual(kwargs["selected_symbols"], ["000001.SZ"])
            return prepared

        def snapshot(*args, **kwargs):
            calls.append(("snapshot", kwargs))

        def generate(**kwargs):
            calls.append(("generate", kwargs))
            return [{"signal_key": "000001.SZ:watch:test"}]

        def load_state(*args, **kwargs):
            calls.append(("state", args[1], kwargs))
            return {"latest_by_key": {}}

        def persist(*args, **kwargs):
            calls.append(("persist", kwargs))
            return [{"symbol": kwargs["symbol"], "state": "confirming"}]

        dependencies = IntradayScanSignalPersistenceDependencies(
            prepare_inputs=prepare_inputs, preparation_dependencies=object(),
            quote_source=lambda _: "tencent_watch_batch", json_safe=lambda value: value,
            persist_rule_input_snapshot=snapshot,
            attach_volume_time_profile=lambda feature, profile, **_: {"feature": feature, "profile": profile},
            number=lambda value: float(value) if value is not None else None,
            aggregate_order_book_observations=lambda rows, _: {"frames": len(rows)},
            generate_signals=generate, signal_generation_dependencies=object(),
            load_event_state=load_state, persist_generated_signals=persist,
            signal_event_persistence_dependencies=object(),
        )
        result = persist_scan_signals(
            connection, scan_id=uuid.uuid4(), observed_at=observed_at, selected_symbols=["000001.SZ"],
            source_status={"tencent": {"status": "completed"}}, watches=[{"symbol": "000001.SZ"}],
            quotes={"000001.SZ": {"price": 10.0, "price_source": "tencent_watch_batch", "raw": {"close": 10}}},
            tencent_rows=[], quote_latency_ms=12, tushare_minutes={"000001.SZ": {"rows": []}},
            surge_features={}, peer_contexts={"000001.SZ": {"peer_count": 1}}, fast_confirmations={},
            confirmation_window=300, signal_model_version="v1", factor_contract_version="v2", dependencies=dependencies,
        )

        self.assertEqual(result, [{"symbol": "000001.SZ", "state": "confirming"}])
        self.assertEqual(len(connection.executed), 1)
        self.assertIn("intraday_quote_observations", connection.executed[0][0])
        self.assertEqual(calls[0][0], "snapshot")
        self.assertEqual(calls[1][0], "generate")
        self.assertEqual(calls[2][1], ["000001.SZ:watch:test"])
        self.assertEqual(calls[3][0], "persist")
        self.assertEqual(calls[3][1]["order_book_feature"], {"frames": 1})
        self.assertEqual(calls[3][1]["candidate_sector_keys"], ["sector-a"])


if __name__ == "__main__":
    unittest.main()
