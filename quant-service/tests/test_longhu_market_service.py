from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace

from app.longhu_market_service import sync


class LonghuMarketServiceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_vendor_failure_persists_reason_without_nameerror(self):
        # Regression: the exception bound by ``except ... as error`` is
        # deleted once the except block exits.  ``fail`` closes over it, so
        # it must capture the fields first or a real (non-mocked) database
        # call raises NameError instead of recording the failure.
        calls: list[tuple[str, tuple]] = []

        class Connection:
            def execute(self, statement, params=None):
                calls.append((statement, params))
                return SimpleNamespace(fetchone=lambda: None)

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_database(action, *args, **kwargs):
            return action(*args)

        async def run_public(_fn, *_args, **_kwargs):
            raise RuntimeError("longhu vendor request failed")

        result = await sync(
            date(2026, 8, 21), db=Database(), run_public_blocking=run_public,
            run_database_blocking=run_database, persist_rows=lambda *_args: 0,
            persist_flow_rows=lambda *_args: 0,
            source_factory=lambda: SimpleNamespace(fetch_full_market_evidence=lambda *_args, **_kwargs: None),
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("longhu vendor request failed", result["reason"])
        failure_updates = [
            params for statement, params in calls
            if "error_class=%s,error_message=%s" in statement
        ]
        self.assertEqual(len(failure_updates), 1)
        self.assertEqual(failure_updates[0][0], "RuntimeError")
        self.assertEqual(failure_updates[0][1], "longhu vendor request failed")


if __name__ == "__main__":
    unittest.main()
