from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace

from app.market_universe_sync import sync


class MarketUniverseSyncFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_persists_reason_without_nameerror(self):
        # Regression: the exception bound by ``except ... as error`` is
        # deleted once the except block exits.  ``persist_failure`` closes
        # over it, so it must capture the text first or a real (non-mocked)
        # database call raises NameError instead of recording the failure.
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

        provider = SimpleNamespace(key="tushare_primary", name="primary")

        async def call(*_args, **_kwargs):
            raise RuntimeError("upstream stock_basic timed out")

        result = await sync(
            SimpleNamespace(universe_key="all_a", provider="auto", minimum_rows=100),
            provider_candidates=lambda *_args: [provider], cn_date=lambda: date(2026, 8, 21),
            call_tushare_api=call, looks_like_response_header=lambda _rows: False,
            persist_tushare_rows=lambda *_args: 0, run_database_blocking=run_database,
            persist_tushare_fetch_blocked=lambda *_args: None, db=Database(),
            safe_error_detail=lambda value, _limit: value,
            provider_call_error=ValueError, executor_saturated_error=KeyError,
            record_provider_success=lambda *_args: None,
            record_provider_failure=lambda *_args: None,
            record_provider_api_capability=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("upstream stock_basic timed out", result["reason"])
        failure_updates = [
            params for statement, params in calls
            if "error_class='provider_error'" in statement
        ]
        self.assertEqual(len(failure_updates), 1)
        self.assertIn("upstream stock_basic timed out", failure_updates[0][0])


if __name__ == "__main__":
    unittest.main()
