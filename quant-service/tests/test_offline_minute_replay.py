"""Causal-clock regression tests for locally supplied minute files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from app.intraday_replay import replay_events
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


if __name__ == "__main__":
    unittest.main()
