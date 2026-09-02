"""Coverage for the transaction consolidation in market_flow_repository.

``rebuild_stored_market_flow_features`` previously opened one transaction per
read plus one further transaction per minute row and per snapshot row
(potentially hundreds across a 45-day window).  It now processes every row
inside the single transaction it reads from.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
import unittest

from app.market_flow_repository import rebuild_stored_market_flow_features


class _Result:
    def __init__(self, *, row=None, rows=None):
        self._row, self._rows = row, rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, minute_rows, snapshot_rows):
        self.minute_rows = minute_rows
        self.snapshot_rows = snapshot_rows
        self.calls: list[str] = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append(normalized)
        if "snapshot_minute>=%s AND snapshot_minute<%s" in normalized:
            return _Result(rows=self.minute_rows)
        if "FROM quant.market_snapshot_runs\n" in sql and "BETWEEN" in normalized:
            return _Result(rows=self.snapshot_rows)
        if "snapshot_minute=%s AND status IN" in normalized:
            return _Result(row={"payload": {"items": []}})
        if "snapshot_minute<=%s AND snapshot_minute>=%s" in normalized:
            return _Result(row=None)
        if "snapshot_minute>=%s AND snapshot_minute<=%s" in normalized:
            return _Result(row=None)
        if "session='close'" in normalized:
            return _Result(row=None)
        if "cadence='minute' AND observed_at<=%s" in normalized:
            return _Result(row=None)
        if "INSERT INTO quant.market_flow_feature_snapshots" in normalized:
            return _Result()
        raise AssertionError(f"unexpected SQL in test double: {normalized}")


class _Transaction:
    enter_count = 0

    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        type(self).enter_count += 1
        return self._connection

    def __exit__(self, *_args):
        return False


class _Database:
    def __init__(self, connection):
        self.connection = connection

    def transaction(self):
        return _Transaction(self.connection)


class RebuildStoredMarketFlowFeaturesTransactionTests(unittest.TestCase):
    def test_all_minute_and_snapshot_rows_share_one_transaction(self) -> None:
        _Transaction.enter_count = 0
        minute_rows = [
            {"snapshot_minute": datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc),
             "observed_at": datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)},
            {"snapshot_minute": datetime(2026, 8, 20, 2, 1, tzinfo=timezone.utc),
             "observed_at": datetime(2026, 8, 20, 2, 1, tzinfo=timezone.utc)},
        ]
        snapshot_rows = [
            {"exchange_date": "2026-08-20", "session": "midday",
             "observed_at": datetime(2026, 8, 20, 3, 30, tzinfo=timezone.utc), "summary": {}},
        ]
        connection = _Connection(minute_rows, snapshot_rows)
        database = _Database(connection)
        with patch(
            "app.market_flow_repository.rebuild_sector_flow_daily_features",
            return_value={"status": "completed"},
        ) as sector_daily:
            result = rebuild_stored_market_flow_features(
                database, datetime(2026, 8, 20).date(), datetime(2026, 8, 20).date(),
            )
        # Exactly one transaction for the whole minute+snapshot rebuild, no
        # matter how many minute/snapshot rows it processes.
        self.assertEqual(_Transaction.enter_count, 1)
        self.assertEqual(result["minute_rows"], 2)
        self.assertEqual(result["snapshot_rows"], 1)
        sector_daily.assert_called_once()
        insert_calls = [sql for sql in connection.calls if "INSERT INTO quant.market_flow_feature_snapshots" in sql]
        self.assertEqual(len(insert_calls), 3)  # 2 minute rows + 1 snapshot row


if __name__ == "__main__":
    unittest.main()
