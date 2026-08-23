from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
import uuid

from app.intraday_scan_repository import IntradaySignalEventState
from app.intraday_signal_event_persistence import (
    IntradaySignalEventPersistenceDependencies,
    persist_generated_signals,
)


class _Result:
    def fetchone(self):
        return {"signal_event_id": "event-1"}


class _Connection:
    def __init__(self):
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query, params):
        self.calls.append((query, params))
        return _Result()


class IntradaySignalEventPersistenceTests(unittest.TestCase):
    def test_persists_confirmed_candidate_with_frozen_policy_contract_and_paper_proposal(self):
        connection = _Connection()
        paper_payloads: list[tuple[object, object]] = []
        gate = SimpleNamespace(allowed=True, target_weight=0.15, reasons=("within limits",), risk_flags=("paper_only",))
        dependencies = IntradaySignalEventPersistenceDependencies(
            paper_risk_gate=lambda **_: gate,
            live_policy_gate=lambda *_: {"allow_confirmation": True, "risk_flags": ["manual_review_required"]},
            classify_setup_state=lambda *_: {"state": "confirmed_setup"},
            factor_contracts=lambda _: [{"factor": "qi5", "training_permitted": False}],
            probability=lambda *_: {"probability": 0.58},
            decision_context=lambda _, probability: {"probability": probability},
            signal_contract=lambda _, observed_at: {"observed_at": observed_at.isoformat()},
            event_state=lambda *_args, **_kwargs: "confirmed",
            ensure_episode=lambda *_args, **_kwargs: {
                "episode_id": "episode-1", "material_state_hash": "hash", "stage": "confirmed",
            },
            attribution=lambda *_: {"source": "test"},
            paper_decision_payload=lambda signal, state, policy, portfolio: {
                "signal_key": signal["signal_key"], "state": state, "policy": policy, "portfolio": portfolio,
            },
            persist_paper_decision=lambda _connection, event_id, payload: paper_payloads.append((event_id, payload)),
        )
        observed_at = datetime(2026, 8, 22, 2, 30, tzinfo=timezone.utc)
        event_state = IntradaySignalEventState({}, {}, None)
        persisted = persist_generated_signals(
            connection, scan_id=uuid.uuid4(), observed_at=observed_at, symbol="000001.SZ",
            watch={"symbol": "000001.SZ"}, quote={"price": 11.2}, daily_factors={"status": "ready"},
            minute_feature={"time": "10:30"}, peer_context={"peer_count": 3}, market_context={"state": "neutral"},
            fast_confirmation={"status": "matched"}, order_book_feature={"qi5": 0.2},
            tushare_minute={"latest": {"time": "10:30"}}, paper_position=None,
            portfolio_snapshot={"drawdown": 0.01}, candidate_sector_keys=("885001.TI",),
            probability_profiles={}, generated_signals=[{
                "signal_key": "000001.SZ:entry:test", "signal_type": "entry", "severity": "warning",
                "score": 80, "conditions": {"setup": "test"}, "risk_flags": [],
            }], existing_event_state=event_state, confirmation_window=timedelta(minutes=5),
            factor_contract_version="v1", dependencies=dependencies,
        )

        self.assertEqual(persisted[0]["state"], "confirmed")
        self.assertEqual(event_state.latest_by_key["000001.SZ:entry:test"]["observed_at"], observed_at)
        self.assertEqual(persisted[0]["conditions"]["policy_gate"]["allow_confirmation"], True)
        self.assertEqual(persisted[0]["conditions"]["signal_contract"]["observed_at"], observed_at.isoformat())
        self.assertEqual(paper_payloads[0][0], "event-1")
        self.assertEqual(len(connection.calls), 1)

    def test_cross_source_mismatch_downgrades_even_if_event_machine_confirms(self):
        connection = _Connection()
        gate = SimpleNamespace(allowed=True, target_weight=0.1, reasons=(), risk_flags=())
        dependencies = IntradaySignalEventPersistenceDependencies(
            paper_risk_gate=lambda **_: gate,
            live_policy_gate=lambda *_: {"allow_confirmation": True, "risk_flags": []},
            classify_setup_state=lambda *_: {}, factor_contracts=lambda _: [], probability=lambda *_: {},
            decision_context=lambda *_: {}, signal_contract=lambda *_: {}, event_state=lambda *_args, **_kwargs: "confirmed",
            ensure_episode=lambda *_args, **_kwargs: None, attribution=lambda *_: {},
            paper_decision_payload=lambda *_: self.fail("mismatch must not create paper proposal"),
            persist_paper_decision=lambda *_: self.fail("mismatch must not persist paper proposal"),
        )
        result = persist_generated_signals(
            connection, scan_id=uuid.uuid4(), observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
            symbol="000001.SZ", watch={}, quote=None, daily_factors={}, minute_feature=None, peer_context=None,
            market_context={}, fast_confirmation={"status": "mismatch"}, order_book_feature={}, tushare_minute=None,
            paper_position=None, portfolio_snapshot={}, candidate_sector_keys=(), probability_profiles={},
            generated_signals=[{"signal_key": "key", "signal_type": "watch", "severity": "info", "score": 1,
                                "conditions": {}, "risk_flags": []}],
            existing_event_state=IntradaySignalEventState({}, {}, None), confirmation_window=timedelta(minutes=5),
            factor_contract_version="v1", dependencies=dependencies,
        )

        self.assertEqual(result[0]["state"], "confirming")
        self.assertIn("realtime_cross_source_price_mismatch", result[0]["risk_flags"])


if __name__ == "__main__":
    unittest.main()
