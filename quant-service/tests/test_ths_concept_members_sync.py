from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from app.ths_concept_members_sync import TRANSIENT_CIRCUIT_OPEN_FAILURE_PREFIX, sync


class _Result:
    def __init__(self, *, rows=None, row=None):
        self._rows = list(rows or [])
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if "SELECT count(*)::int total" in sql:
            return _Result(row={"total": 1})
        return _Result(rows=[])


class _Transaction:
    def __init__(self, connection: _Connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args):
        return False


class _Database:
    def __init__(self):
        self.connection = _Connection()

    def transaction(self):
        return _Transaction(self.connection)


class ThsConceptMemberSyncTests(unittest.TestCase):
    def test_resume_retries_circuit_open_failure_after_cooldown(self) -> None:
        database = _Database()
        request = SimpleNamespace(
            provider="auto", refresh_flow_catalog=False, trade_date=date(2026, 8, 14),
            resume=True, member_limit=1, member_offset=0,
        )

        async def run_database_blocking(function, *_args, **_kwargs):
            return function()

        async def unused(*_args, **_kwargs):
            raise AssertionError("no member request should run when the fake selection is empty")

        result = __import__("asyncio").run(sync(
            request,
            sync_flow_catalog=unused, flow_request=object,
            run_database_blocking=run_database_blocking, db=database,
            fetch_catalog=unused, catalog_request=object, load_rows=unused,
            persist_members=lambda *_args: 0,
            observed_at=lambda: datetime.now(timezone.utc), http_exception=HTTPException,
        ))

        self.assertEqual(result["status"], "completed")
        selection_sql, selection_params = database.connection.calls[0]
        self.assertIn("state.last_error LIKE %s", selection_sql)
        self.assertEqual(selection_params, (
            date(2026, 8, 14), f"{TRANSIENT_CIRCUIT_OPEN_FAILURE_PREFIX}%", 1,
        ))


if __name__ == "__main__":
    unittest.main()
