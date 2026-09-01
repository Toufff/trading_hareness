from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from app.research_storage_admission import ResearchStorageAdmission, governance


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class ResearchStorageAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_capture_decision_is_cached_and_never_changes_core_default(self) -> None:
        status = {"allow_nonessential_high_frequency": False, "state": "stop"}
        run_database = AsyncMock(return_value=status)
        admission = ResearchStorageAdmission(lambda: status, run_database, cache_seconds=60)

        first = await admission.optional_high_frequency_allowed()
        second = await admission.optional_high_frequency_allowed()

        self.assertEqual(first, (False, status))
        self.assertEqual(second, (False, status))
        self.assertEqual(run_database.await_count, 1)
        self.assertEqual(run_database.await_args.kwargs["timeout_seconds"], 10)

    async def test_governance_uses_managed_database_and_artifact_budgets(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"bytes": 100}
        database = MagicMock()
        database.transaction.return_value = _Transaction(connection)

        result = governance(
            database,
            environ={
                "QUANT_DATA_DIR": "/tmp/quant-test", "QUANT_RESEARCH_STORAGE_SOFT_BYTES": "1000",
                "QUANT_HOT_DATABASE_SOFT_BYTES": "800", "QUANT_RESEARCH_STORAGE_WARNING_RATIO": "0.8",
                "QUANT_RESEARCH_STORAGE_STOP_RATIO": "0.9",
            },
            directory_bytes=lambda path: 200 if path.as_posix() == "/tmp/quant-test" else 0,
        )

        self.assertEqual(result["managed"]["used_bytes"], 300)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["allow_nonessential_high_frequency"])
