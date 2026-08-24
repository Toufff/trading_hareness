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


if __name__ == "__main__":
    unittest.main()
