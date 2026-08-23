from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.async_analyst_sync_health_repository import sync_health


class _Result:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *_args):
        return False


class _AsyncDatabase:
    def __init__(self, now):
        self.now = now
        self.statements = []

    def transaction(self):
        return _Transaction(self)

    async def execute(self, sql, _params=()):
        statement = str(sql)
        self.statements.append(statement)
        if "analyst_sync_cursors" in statement:
            return _Result([{"stream_key": "reports", "updated_at": self.now}])
        if "analyst_global_sync_cursors" in statement:
            return _Result([{"stream_key": "message_updates", "updated_at": self.now}])
        if "analyst_sync_attempts" in statement:
            return _Result([
                {"stream_key": "reports", "status": "completed", "completed_at": self.now,
                 "error_code": None, "summary": {"workflow_id": "remoteArchiveReports123"}},
                {"stream_key": "messages", "status": "completed", "completed_at": self.now,
                 "error_code": None, "summary": {"workflow_id": "remoteArchiveMessages123"}},
            ])
        if "analyst_promotion_registry" in statement:
            return _Result([])
        if "public.workflow_entity" in statement:
            return _Result([
                {"id": "remoteArchiveReports123", "active": True, "published": True,
                 "active_version_id": "current", "latest_execution_version_id": "retired",
                 "latest_execution_status": "error"},
                {"id": "remoteArchiveMessages123", "active": True, "published": True,
                 "active_version_id": "current", "latest_execution_version_id": "retired",
                 "latest_execution_status": "error"},
            ])
        raise AssertionError(f"unexpected SQL: {statement}")


class AsyncAnalystSyncHealthRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_workflow_receipts_override_old_execution_rows(self) -> None:
        database = _AsyncDatabase(datetime.now(timezone.utc))

        payload = await sync_health(database)

        self.assertEqual(payload["runtime_verification"], "verified_recent_execution")
        self.assertEqual({item["status"] for item in payload["workflow_health"]}, {"ready"})
        self.assertEqual(
            {item["execution_evidence"] for item in payload["workflow_health"]},
            {"current_workflow_sync_receipt"},
        )
        self.assertTrue(any("public.workflow_entity" in statement for statement in database.statements))
