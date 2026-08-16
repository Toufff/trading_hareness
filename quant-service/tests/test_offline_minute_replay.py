"""Causal-clock regression tests for locally supplied minute files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from app.intraday_replay import replay_events
from app.intraday_rule_inputs import (
    INTRADAY_RULE_INPUT_SCHEMA_VERSION,
    intraday_rule_input_hash,
    intraday_rule_input_payload,
    intraday_rule_replay_inputs,
)
from app.intraday_rule_snapshot_repository import prune_rule_input_evidence
from app.intraday_signal_rules import signal_rules
from app.offline_minute_replay import offline_minute_market_event, offline_minute_replay_events


UTC = timezone.utc


def minute_row(*, symbol: str = "000001.SZ", bar_second: int = 0, available_second: int = 5,
               local_second: int = 9) -> dict[str, object]:
    base = datetime(2026, 8, 10, 1, 30, tzinfo=UTC)
    return {
        "symbol": symbol,
        "bar_time": base + timedelta(seconds=bar_second),
        "source_available_at": base + timedelta(seconds=available_second),
        "available_at": base + timedelta(seconds=local_second),
        "open": Decimal("10.00"), "high": Decimal("10.20"), "low": Decimal("9.90"),
        "close": Decimal("10.10"), "volume": Decimal("100"), "amount": Decimal("1010"),
        "source_name": "mounted-csv", "import_id": "f9ca6765-1bcd-4ed0-a901-45a4a59e7bee",
    }


class OfflineMinuteReplayTests(unittest.TestCase):
    def test_event_uses_vendor_clock_not_local_import_clock(self) -> None:
        row = minute_row()
        event = offline_minute_market_event(row)
        self.assertEqual(event.event_time, row["bar_time"])
        self.assertEqual(event.available_at, row["source_available_at"])
        self.assertEqual(event.ingested_at, row["available_at"])
        self.assertEqual(event.source, "offline_minute_bar")
        self.assertIn("offline_source_availability_replay_only", event.quality_flags)

    def test_absent_or_impossible_vendor_clock_is_rejected(self) -> None:
        missing = minute_row()
        missing["source_available_at"] = None
        with self.assertRaisesRegex(ValueError, "source_available_at"):
            offline_minute_market_event(missing)
        impossible = minute_row(available_second=10, local_second=9)
        with self.assertRaisesRegex(ValueError, "cannot be after"):
            offline_minute_market_event(impossible)

    def test_event_order_is_availability_order_and_deterministic(self) -> None:
        first_in_file = minute_row(symbol="000002.SZ", available_second=6)
        second_in_file = minute_row(symbol="000001.SZ", available_second=5)
        events = offline_minute_replay_events([first_in_file, second_in_file])
        self.assertEqual([item[0].symbol for item in events], ["000001.SZ", "000002.SZ"])
        self.assertEqual([item[1] for item in events], [1, 2])

        def transition(state, event, _payload):
            return {"seen": [*(state.get("seen") or []), event.symbol]}, {"symbol": event.symbol}

        first = replay_events(events, transition)
        second = replay_events(offline_minute_replay_events([second_in_file, first_in_file]), transition)
        self.assertEqual(first["state"], {"seen": ["000001.SZ", "000002.SZ"]})
        self.assertEqual(first["digest"], second["digest"])


class IntradayRuleInputSnapshotTests(unittest.TestCase):
    def test_frozen_inputs_replay_the_same_pure_rule_and_exclude_raw_payload(self) -> None:
        watch = {"symbol": "000001.SZ", "alert_on_entry": True, "alert_on_exit": True,
                 "metadata": {}, "entry_price": None, "raw": {"never": "persist"}}
        quote = {"symbol": "000001.SZ", "price": Decimal("10.30"), "pct_change": Decimal("3.0"),
                 "volume_ratio": Decimal("2.0"), "turnover_rate": Decimal("3.0"),
                 "main_net_inflow": Decimal("100"), "main_flow_percentile": Decimal("0.9"),
                 "raw": {"vendor": "unbounded"}}
        previous = {"symbol": "000001.SZ", "source_name": "tencent_free", "price": Decimal("10.0"),
                    "observed_at": datetime(2026, 8, 10, 1, 30, tzinfo=UTC)}
        daily = {"status": "completed", "ma_trend": "bullish"}
        minute = {"return_1m_pct": 0.4, "return_3m_pct": 1.3, "minute_volume_multiple": 3.0,
                  "above_vwap_pct": 0.5, "breakout_above_prior_high_pct": 0.1,
                  "session_range_position": 0.9, "time": "10:00"}
        peers = {"confirming_peer_count": 2, "available_peer_count": 2, "confirming_breadth": 1.0}
        payload = intraday_rule_input_payload(
            watch=watch, quote=quote, previous_quote=previous, daily_factors=daily,
            minute_features=minute, peer_context=peers, model_version="test-v1",
        )
        self.assertEqual(payload["schema_version"], INTRADAY_RULE_INPUT_SCHEMA_VERSION)
        self.assertNotIn("raw", payload["quote"])
        self.assertNotIn("raw", payload["watch"])
        self.assertEqual(intraday_rule_input_hash(payload), intraday_rule_input_hash(dict(payload)))
        restored = intraday_rule_replay_inputs(payload, expected_model_version="test-v1")

        def assessment(_quote, _daily, _minute, _peers):
            return {"status": "not_confirmed", "score": 0, "components": {}, "risk_flags": []}

        original = signal_rules(watch, quote, previous, daily, minute, peers,
                                number=lambda value: float(value) if value is not None else None,
                                upside_assessment_fn=assessment, model_version="test-v1")
        replayed = signal_rules(restored["watch"], restored["quote"], restored["previous_quote"],
                                restored["daily_factors"], restored["minute_features"], restored["peer_context"],
                                number=lambda value: float(value) if value is not None else None,
                                upside_assessment_fn=assessment, model_version=restored["model_version"])
        self.assertEqual([item["signal_key"] for item in replayed], [item["signal_key"] for item in original])

    def test_snapshot_version_mismatch_and_retention_are_explicit(self) -> None:
        payload = intraday_rule_input_payload(
            watch={"symbol": "000001.SZ"}, quote=None, previous_quote=None,
            daily_factors={}, minute_features={}, peer_context={}, model_version="test-v1",
        )
        with self.assertRaisesRegex(ValueError, "model version"):
            intraday_rule_replay_inputs(payload, expected_model_version="test-v2")

        class Connection:
            def __init__(self): self.calls = []
            def execute(self, sql, params): self.calls.append((sql, params))

        connection = Connection()
        cutoff = datetime(2026, 8, 1, tzinfo=UTC)
        prune_rule_input_evidence(connection, cutoff=cutoff)
        self.assertEqual(len(connection.calls), 2)
        self.assertIn("intraday_rule_input_snapshots", connection.calls[0][0])
        self.assertIn("tencent_free", connection.calls[1][0])
        self.assertEqual(connection.calls[0][1], (cutoff,))


if __name__ == "__main__":
    unittest.main()
