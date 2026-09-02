from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace

from app.full_market_daily_sync import sync


class FullMarketDailySyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_falls_back_when_first_source_is_nonempty_but_incomplete(self):
        class Connection:
            def execute(self, _statement, _params=None):
                return SimpleNamespace(fetchone=lambda: None)

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

            # WP6: the successful persist path opted into the long-task
            # statement_timeout budget via ``long_transaction`` instead of
            # the default-timeout ``transaction``.
            long_transaction = transaction

        database_timeouts: list[int | None] = []

        async def run_database(action, *args, **kwargs):
            database_timeouts.append(kwargs.get("timeout_seconds"))
            return action(*args)

        attempted: list[str] = []
        rejected: list[tuple[str, str]] = []
        super_get = SimpleNamespace(key="tushare_super_get", name="super_get")
        primary = SimpleNamespace(key="tushare_primary", name="primary")

        def rows(count: int) -> list[dict[str, object]]:
            return [
                {"ts_code": f"{index:06d}.SZ", "trade_date": date(2026, 8, 21)}
                for index in range(1, count + 1)
            ]

        async def call(_api, _params, _fields, preference):
            attempted.append(preference)
            provider = super_get if preference == "super_get" else primary
            return SimpleNamespace(
                provider=provider, rows=rows(2 if preference == "super_get" else 5),
                failed_providers=(), empty_providers=(),
            )

        result = await sync(
            SimpleNamespace(trade_date=date(2026, 8, 21), provider="auto", minimum_rows=5),
            provider_candidates=lambda *_args: [super_get, primary], cn_date=lambda: date(2026, 8, 21),
            call_tushare_api=call, looks_like_response_header=lambda _rows: False,
            tushare_date=lambda value: value, persist_tushare_rows=lambda *_args: 5,
            run_database_blocking=run_database, persist_tushare_fetch_blocked=lambda *_args: None,
            db=Database(), safe_error_detail=lambda value, _limit: value,
            provider_call_error=RuntimeError, executor_saturated_error=RuntimeError,
            record_provider_success=lambda *_args: None,
            record_provider_failure=lambda _connection, provider, _api, error, _latency: rejected.append((provider, error)),
            record_provider_api_capability=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider"], "tushare_primary")
        self.assertEqual(attempted, ["super_get", "primary"])
        self.assertEqual(rejected, [("tushare_super_get", "daily returned 2 valid A-share rows; expected at least 5")])
        self.assertEqual(database_timeouts, [None, 180])

    async def test_minimum_row_gate_participates_in_idempotency_key(self):
        class Connection:
            def execute(self, statement, _params):
                self.statement = statement
                return SimpleNamespace(fetchone=lambda: {"status": "completed", "row_count": 3_400})

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_database(action, *args, **_kwargs):
            return action(*args)

        async def unchanged_key(minimum_rows: int) -> str:
            result = await sync(
                SimpleNamespace(trade_date=date(2026, 8, 21), provider="auto", minimum_rows=minimum_rows),
                provider_candidates=lambda *_args: [SimpleNamespace(key="provider")],
                cn_date=lambda: date(2026, 8, 21), call_tushare_api=None,
                looks_like_response_header=lambda _rows: False, tushare_date=lambda _value: None,
                persist_tushare_rows=lambda *_args: 0, run_database_blocking=run_database,
                persist_tushare_fetch_blocked=lambda *_args: None, db=Database(), safe_error_detail=lambda value, _limit: value,
                provider_call_error=RuntimeError, executor_saturated_error=RuntimeError,
                record_provider_success=lambda *_args: None, record_provider_failure=lambda *_args: None,
                record_provider_api_capability=lambda *_args, **_kwargs: None,
            )
            self.assertEqual(result["status"], "unchanged")
            return result["request_key"]

        self.assertNotEqual(await unchanged_key(3_400), await unchanged_key(5_000))

    async def test_source_empty_across_all_candidates_persists_reason_without_nameerror(self):
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

        async def call(_api, _params, _fields, _preference):
            return SimpleNamespace(provider=provider, rows=[], failed_providers=(), empty_providers=())

        result = await sync(
            SimpleNamespace(trade_date=date(2026, 8, 21), provider="auto", minimum_rows=5),
            provider_candidates=lambda *_args: [provider], cn_date=lambda: date(2026, 8, 21),
            call_tushare_api=call, looks_like_response_header=lambda _rows: False,
            tushare_date=lambda value: value, persist_tushare_rows=lambda *_args: 0,
            run_database_blocking=run_database, persist_tushare_fetch_blocked=lambda *_args: None,
            db=Database(), safe_error_detail=lambda value, _limit: value,
            provider_call_error=RuntimeError, executor_saturated_error=ValueError,
            record_provider_success=lambda *_args: None,
            record_provider_failure=lambda *_args: None,
            record_provider_api_capability=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("tushare_primary: daily returned no rows", result["reason"])
        failure_updates = [params for statement, params in calls if "error_class=%s,error_message=%s" in statement]
        self.assertEqual(len(failure_updates), 1)
        self.assertEqual(failure_updates[0][0], "source_empty")
        self.assertIn("daily returned no rows", failure_updates[0][1])

    async def test_all_empty_sources_use_record_provider_empty_when_injected(self) -> None:
        """WP6: rows==0 must not reset the provider's failure streak.

        Historically ``record_provider_success(..., 0)`` was called for every
        source that legitimately returned zero rows, which reset
        ``consecutive_failures`` even for a genuinely failing provider. The
        dedicated ``record_provider_empty`` callback (when injected) must be
        used instead, and ``record_provider_success`` must not be called.
        """
        class Connection:
            def execute(self, _statement, _params=None):
                return SimpleNamespace(fetchone=lambda: None)

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_database(action, *args, **_kwargs):
            return action(*args)

        provider = SimpleNamespace(key="tushare_primary", name="primary")

        async def call(_api, _params, _fields, _preference):
            return SimpleNamespace(provider=provider, rows=[], failed_providers=(), empty_providers=())

        empty_calls: list[tuple[str, str]] = []
        success_calls: list[tuple] = []

        result = await sync(
            SimpleNamespace(trade_date=date(2026, 8, 21), provider="auto", minimum_rows=5),
            provider_candidates=lambda *_args: [provider], cn_date=lambda: date(2026, 8, 21),
            call_tushare_api=call, looks_like_response_header=lambda _rows: False,
            tushare_date=lambda value: value, persist_tushare_rows=lambda *_args: 0,
            run_database_blocking=run_database, persist_tushare_fetch_blocked=lambda *_args: None,
            db=Database(), safe_error_detail=lambda value, _limit: value,
            provider_call_error=RuntimeError, executor_saturated_error=ValueError,
            record_provider_success=lambda *args: success_calls.append(args),
            record_provider_failure=lambda *_args: None,
            record_provider_api_capability=lambda *_args, **_kwargs: None,
            record_provider_empty=lambda _connection, provider_key, capability: empty_calls.append((provider_key, capability)),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(empty_calls, [("tushare_primary", "daily_all_a")])
        self.assertEqual(success_calls, [])


if __name__ == "__main__":
    unittest.main()
