"""Instrument registration contract for the bounded minute capture action.

The capture used to call the injected per-symbol ``ensure_instrument`` inside
its persistence loop, i.e. one ``INSERT INTO quant.instruments ... ON CONFLICT``
per watchlist symbol inside the same transaction that writes the minute rows.
It now registers the whole captured basket in one batched call.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

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


def _capture(symbols, fetch_minutes, registered, parse_minute=None):
    database = _RecordingDatabase()
    actions = IntradayMinuteCaptureActions(database)
    result = asyncio.run(actions.capture(
        symbols,
        realtime_session=_open_session,
        fetch_minutes=fetch_minutes,
        run_database=_run_database,
        parse_minute=parse_minute or (lambda row: (_ for _ in ()).throw(AssertionError("no rows expected"))),
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


class IntradayMinuteCaptureSuccessPathTests(unittest.TestCase):
    """Cover the persistence loop itself, not only the registration call.

    The refactor moved the instrument registration across a transaction
    boundary and left the rest of the loop -- the per-row
    ``intraday_minute_sessions`` upsert, the ``stored_by_symbol`` tally and
    the one retention DELETE per captured symbol -- with no test that ever
    executes it: the other tests in this file return no rows and assert
    ``status == 'failed'``.
    """

    @staticmethod
    def _parse_minute(row: dict[str, Any]) -> dict[str, Any]:
        """Stand in for ``offline_minute_row``: it must return a bar whose
        Shanghai-local date is the capture's ``trading_date``, otherwise the
        action skips the row."""
        local_now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        hour, minute = (int(part) for part in str(row["datetime"]).split(" ")[1].split(":"))
        bar_time = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        close = Decimal(str(row["close"]))
        return {
            "bar_time": bar_time, "open": close, "high": close, "low": close, "close": close,
            "volume": Decimal(str(row.get("volume") or 0)), "amount": Decimal(str(row.get("amount") or 0)),
            "raw": row,
        }

    def test_captured_rows_are_stored_and_each_symbol_gets_one_retention_delete(self) -> None:
        registered: list[list[str]] = []
        rows_by_symbol = {
            "600000.SH": [
                {"time": "1455", "close": "12.0", "volume": "100"},
                {"time": "1456", "close": "12.1", "volume": "200"},
            ],
            "000001.SZ": [{"time": "1455", "close": "10.5", "volume": "300"}],
        }

        async def fetch_minutes(symbol: str) -> list[dict[str, Any]]:
            return rows_by_symbol[symbol]

        result, database = _capture(
            ["600000.SH", "000001.SZ"], fetch_minutes, registered, parse_minute=self._parse_minute,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["errors"], {})
        self.assertEqual(result["stored"], 3)
        self.assertEqual(result["stored_by_symbol"], {"600000.SH": 2, "000001.SZ": 1})

        calls = database.connection.calls
        self.assertEqual(registered, [["600000.SH", "000001.SZ"]])
        inserts = [params for sql, params in calls if "INSERT INTO quant.intraday_minute_sessions" in sql]
        self.assertEqual(len(inserts), 3)
        self.assertEqual([params[0] for params in inserts], ["600000.SH", "600000.SH", "000001.SZ"])
        # Minute buckets come from the parsed bar time, not from the raw field.
        self.assertEqual([params[2] for params in inserts], ["14:55", "14:56", "14:55"])

        deletes = [params for sql, params in calls if "DELETE FROM quant.intraday_minute_sessions" in sql]
        self.assertEqual([params[0] for params in deletes], ["600000.SH", "000001.SZ"])
        trading_date = date.fromisoformat(result["trading_date"])
        for params in deletes:
            self.assertEqual(params[1], trading_date - timedelta(days=result["retention_days"]))

    def test_one_invalid_row_is_isolated_and_the_rest_of_the_symbol_still_stores(self) -> None:
        registered: list[list[str]] = []

        def parse_minute(row: dict[str, Any]) -> dict[str, Any]:
            if row.get("close") == "bad":
                raise ValueError("close is not a number")
            return self._parse_minute(row)

        async def fetch_minutes(_symbol: str) -> list[dict[str, Any]]:
            return [
                {"time": "1455", "close": "bad"},
                {"time": "1456", "close": "12.1", "volume": "200"},
            ]

        result, database = _capture(["600000.SH"], fetch_minutes, registered, parse_minute=parse_minute)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stored_by_symbol"], {"600000.SH": 1})
        self.assertIn("invalid minute row", result["errors"]["600000.SH"])
        inserts = [params for sql, params in database.connection.calls
                   if "INSERT INTO quant.intraday_minute_sessions" in sql]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][2], "14:56")


if __name__ == "__main__":
    unittest.main()
