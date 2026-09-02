"""Coverage for the savepoint fix in async_analyst_sync_health_repository.

A bare ``except Exception`` around the optional n8n-audit read used to leave
the whole transaction aborted on failure (PostgreSQL turns the eventual
COMMIT into a silent ROLLBACK), discarding the cursor/attempt reads captured
earlier in the same call even though they had already succeeded.  The n8n
read now runs inside its own nested ``connection.transaction()`` (a
SAVEPOINT), so a failure there is isolated from the surrounding transaction.
"""

from __future__ import annotations

import unittest

from app.analyst_sync_health_projection import WORKFLOW_SQL
from app.async_analyst_sync_health_repository import sync_health


class _Result:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _Savepoint:
    """Records that a nested ``connection.transaction()`` (SAVEPOINT) was used."""

    entered = 0
    exited_with_exception = 0

    async def __aenter__(self):
        type(self).entered += 1
        return self

    async def __aexit__(self, exc_type, exc, _tb):
        if exc_type is not None:
            type(self).exited_with_exception += 1
        return False  # never swallow here; the caller's own try/except does that


class _Connection:
    def __init__(self, workflow_should_fail: bool):
        self.workflow_should_fail = workflow_should_fail
        self.calls: list[str] = []

    def transaction(self):
        return _Savepoint()

    async def execute(self, sql, params=None):
        self.calls.append(sql)
        if sql is WORKFLOW_SQL and self.workflow_should_fail:
            raise RuntimeError("relation \"public.workflow_entity\" does not exist")
        return _Result([{"stream_key": "reports"}])


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *_args):
        return False


class _Database:
    def __init__(self, connection):
        self.connection = connection

    def transaction(self):
        return _Transaction(self.connection)


class AsyncAnalystSyncHealthSavepointTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_failure_uses_a_nested_savepoint_and_still_returns_local_receipts(self) -> None:
        _Savepoint.entered = 0
        _Savepoint.exited_with_exception = 0
        connection = _Connection(workflow_should_fail=True)
        payload = await sync_health(_Database(connection))
        self.assertEqual(_Savepoint.entered, 1)
        self.assertEqual(_Savepoint.exited_with_exception, 1)
        # The local receipts read before the failed optional read must still
        # come through in the projection.
        self.assertIn("cursors", payload)

    async def test_no_savepoint_needed_when_workflow_read_succeeds(self) -> None:
        _Savepoint.entered = 0
        _Savepoint.exited_with_exception = 0
        connection = _Connection(workflow_should_fail=False)
        await sync_health(_Database(connection))
        self.assertEqual(_Savepoint.entered, 1)
        self.assertEqual(_Savepoint.exited_with_exception, 0)


if __name__ == "__main__":
    unittest.main()
