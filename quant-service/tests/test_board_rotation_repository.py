from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import unittest

from app.board_rotation_repository import BoardRotationRepository


class _Result:
    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, current, previous, pending):
        self.current = current
        self.previous = previous
        self.pending = pending
        self.inserted: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, query, params=None):
        text = str(query)
        if "WHERE snapshot_minute=%s" in text:
            return _Result(row=self.current)
        if "WHERE snapshot_minute<%s" in text:
            return _Result(row=self.previous)
        if "SELECT * FROM quant.intraday_board_rotation_events" in text:
            return _Result(rows=self.pending)
        if "RETURNING *" in text:
            return _Result(row={"rotation_event_id": "confirmed-1", "state": "confirmed"})
        if "SELECT state FROM quant.intraday_board_rotation_events" in text:
            return _Result(row=None)
        if "INSERT INTO quant.intraday_board_rotation_events" in text:
            self.inserted.append((text, params))
        return _Result()


class _Database:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        yield self.connection


class BoardRotationRepositoryTests(unittest.TestCase):
    def test_confirms_previous_direction_and_creates_new_candidate_without_provider_access(self) -> None:
        previous_minute = datetime(2026, 8, 11, 1, 1, tzinfo=timezone.utc)
        current_minute = datetime(2026, 8, 11, 1, 2, tzinfo=timezone.utc)
        current = {"snapshot_minute": current_minute, "payload": {"items": [{"sector_key": "A"}]}}
        previous = {"snapshot_minute": previous_minute, "payload": {"items": [{"sector_key": "A"}]}}
        pending = [{
            "rotation_event_id": "pending-1", "event_key": "existing", "snapshot_minute": previous_minute,
            "conditions": {"sector_key": "A"},
        }]
        connection = _Connection(current, previous, pending)
        repository = BoardRotationRepository(_Database(connection))
        candidate = {
            "event_key": "new", "taxonomy_key": "eastmoney_concept", "sector_key": "B", "label": "板块B",
            "event_type": "cross_zero", "direction": "inflow",
        }

        confirmed = repository.evaluate(
            current_minute, current_minute,
            candidates_for=lambda prior, current_items: [candidate],
            still_directional=lambda event, current_items: event["sector_key"] == "A",
        )

        self.assertEqual(confirmed, [{"rotation_event_id": "confirmed-1", "state": "confirmed"}])
        self.assertEqual(len(connection.inserted), 1)
        self.assertEqual(connection.inserted[0][1][1], "new")

    def test_missing_adjacent_snapshot_fails_closed_without_creating_event(self) -> None:
        current_minute = datetime(2026, 8, 11, 1, 2, tzinfo=timezone.utc)
        connection = _Connection(None, None, [])
        repository = BoardRotationRepository(_Database(connection))

        confirmed = repository.evaluate(
            current_minute, current_minute,
            candidates_for=lambda prior, current: (_ for _ in ()).throw(AssertionError("must not inspect missing snapshots")),
            still_directional=lambda event, current: False,
        )

        self.assertEqual(confirmed, [])
        self.assertEqual(connection.inserted, [])


if __name__ == "__main__":
    unittest.main()
