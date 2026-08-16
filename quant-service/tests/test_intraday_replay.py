"""Deterministic event-clock regression coverage."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.intraday_replay import market_event_from_row, replay_events


class IntradayReplayTests(unittest.TestCase):
    def test_replay_orders_on_availability_then_sequence_and_is_repeatable(self) -> None:
        later_event = market_event_from_row({
            "symbol": "000001.SZ", "observed_at": datetime(2026, 8, 10, 1, 1, tzinfo=timezone.utc),
            "ingested_at": datetime(2026, 8, 10, 1, 1, 1, tzinfo=timezone.utc), "raw": {"price": 11},
        }, source="quote", schema_version="v1")
        earlier_event = market_event_from_row({
            "symbol": "000001.SZ", "observed_at": datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc),
            "ingested_at": datetime(2026, 8, 10, 1, 0, 1, tzinfo=timezone.utc), "raw": {"price": 10},
        }, source="quote", schema_version="v1")

        def transition(state, event, payload):
            seen = list(state.get("seen") or [])
            seen.append(payload["price"])
            return {"seen": seen}, {"price": payload["price"]}

        events = [(later_event, 0, {"price": 11}), (earlier_event, 0, {"price": 10})]
        first, second = replay_events(events, transition), replay_events(events, transition)
        self.assertEqual(first["state"]["seen"], [10, 11])
        self.assertEqual(first["digest"], second["digest"])
        self.assertIn("no provider access", first["policy"])

    def test_replay_rejects_an_impossible_availability_clock(self) -> None:
        event = market_event_from_row({
            "symbol": "000001.SZ", "observed_at": datetime(2026, 8, 10, 1, 1, tzinfo=timezone.utc),
            "available_at": datetime(2026, 8, 10, 1, 2, tzinfo=timezone.utc),
            "ingested_at": datetime(2026, 8, 10, 1, 1, tzinfo=timezone.utc), "raw": {},
        }, source="quote", schema_version="v1")
        with self.assertRaisesRegex(ValueError, "available_at"):
            replay_events([(event, 0, {})], lambda state, _event, _payload: (state, None))


if __name__ == "__main__":
    unittest.main()
