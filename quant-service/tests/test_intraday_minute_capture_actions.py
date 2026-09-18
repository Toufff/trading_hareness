"""Instrument registration contract for the bounded minute capture action.

The capture used to call the injected per-symbol ``ensure_instrument`` inside
its persistence loop, i.e. one ``INSERT INTO quant.instruments ... ON CONFLICT``
per watchlist symbol inside the same transaction that writes the minute rows.
It now registers the whole captured basket in one batched call.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from app.intraday_minute_capture_actions import IntradayMinuteCaptureActions


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return None


class _RecordingDatabase:
    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    def transaction(self):
        database = self

        class _Transaction:
            def __enter__(self) -> _RecordingConnection:
                return database.connection

            def __exit__(self, *_args: Any) -> None:
                return None

        return _Transaction()


async def _run_database(function, timeout_seconds: int = 60):
    return function()


def _capture(symbols, fetch_minutes, registered):
    database = _RecordingDatabase()
    actions = IntradayMinuteCaptureActions(database)
    result = asyncio.run(actions.capture(
        symbols,
        realtime_session=_open_session,
        fetch_minutes=fetch_minutes,
        run_database=_run_database,
        parse_minute=lambda row: (_ for _ in ()).throw(AssertionError("no rows expected")),
        ensure_instruments=lambda _connection, values: registered.append(list(values)),
        retention_days=lambda: 30,
    ))
    return result, database


async def _open_session() -> tuple[bool, str]:
    return True, "open"


class IntradayMinuteCaptureInstrumentBatchingTests(unittest.TestCase):
    def test_whole_basket_is_registered_in_one_call(self) -> None:
        registered: list[list[str]] = []

        async def fetch_minutes(_symbol: str) -> list[dict[str, Any]]:
            return []

        result, _database = _capture(["600000.SH", "000001.SZ"], fetch_minutes, registered)
        self.assertEqual(result["status"], "failed")  # no rows returned by the stub provider
        self.assertEqual(registered, [["600000.SH", "000001.SZ"]])

    def test_failed_symbols_are_not_registered(self) -> None:
        registered: list[list[str]] = []

        async def fetch_minutes(symbol: str) -> list[dict[str, Any]]:
            if symbol == "600000.SH":
                raise RuntimeError("provider timeout")
            return []

        result, _database = _capture(["600000.SH", "000001.SZ"], fetch_minutes, registered)
        self.assertIn("600000.SH", result["errors"])
        self.assertEqual(registered, [["000001.SZ"]])


if __name__ == "__main__":
    unittest.main()
