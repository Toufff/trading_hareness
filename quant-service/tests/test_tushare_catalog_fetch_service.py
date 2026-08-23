import asyncio
from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from app.runtime_executors import ExecutorSaturatedError
from app.tushare_catalog_fetch_service import CatalogFetchDependencies, fetch_catalog


class CatalogFetchServiceTests(unittest.TestCase):
    @staticmethod
    def request(api_name: str = "daily") -> SimpleNamespace:
        return SimpleNamespace(
            api_name=api_name,
            provider="super",
            params={"ts_code": "000001.SZ"},
            fields="",
            paginate=False,
            page_size=1000,
            max_rows=10,
            max_pages=1,
            require_complete=True,
            force_refresh=False,
        )

    @staticmethod
    def dependencies(call_api, calls: list[str], persisted_rows: list[dict[str, object]] | None = None) -> CatalogFetchDependencies:
        provider = SimpleNamespace(key="tushare_super_get")

        async def market_session(_: str) -> tuple[bool, str]:
            return True, "continuous auction"

        async def circuit_open(_: str, __: list[object]) -> set[str]:
            return set()

        def prepare(*_: object) -> None:
            return None

        def success(*args: object) -> tuple[str, int]:
            if persisted_rows is not None:
                persisted_rows.extend(args[2])
            return "completed", 1

        def cancel(*_: object) -> None:
            return None

        def failure(*_: object) -> None:
            return None

        def blocked(*_: object) -> None:
            return None

        async def run_database(callback, *args: object, **__: object):
            calls.append(callback.__name__)
            return callback(*args)

        return CatalogFetchDependencies(
            realtime_market_hours_apis={"rt_min"},
            realtime_market_session=market_session,
            provider_candidates=lambda *_: [provider],
            circuit_open_provider_keys=circuit_open,
            run_database=run_database,
            prepare_run=prepare,
            persist_success=success,
            persist_cancel=cancel,
            persist_failure=failure,
            persist_blocked=blocked,
            call_api=call_api,
            looks_like_response_header=lambda _: False,
            realtime_rows_are_current=lambda *_: True,
            catalog={"daily": "daily", "rt_min": "realtime"},
            normalized_apis={"daily"},
            provider_call_error=RuntimeError,
            executor_saturated_error=ExecutorSaturatedError,
            local_capacity_detail="local processing capacity is temporarily saturated; retry shortly",
        )

    def test_persists_sorted_bounded_success_through_injected_ledger(self):
        async def check():
            calls: list[str] = []
            persisted_rows: list[dict[str, object]] = []

            async def call_api(*_: object, **__: object):
                return SimpleNamespace(
                    rows=[
                        {"ts_code": "000001.SZ", "time": "2026-08-22 09:31:00"},
                        {"ts_code": "000001.SZ", "time": "2026-08-22 09:32:00"},
                    ],
                    complete=True,
                    provider=SimpleNamespace(key="tushare_super_get"),
                    failed_providers=(),
                    empty_providers=(),
                    pages=1,
                )

            request = self.request("rt_min")
            request.max_rows = 1
            request.require_complete = False
            outcome = await fetch_catalog(request, self.dependencies(call_api, calls, persisted_rows))
            return outcome, calls, persisted_rows

        outcome, calls, persisted_rows = asyncio.run(check())
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(outcome["received"], 2)
        self.assertEqual(outcome["stored"], 1)
        self.assertEqual(outcome["provider"], "tushare_super_get")
        self.assertEqual(persisted_rows, [{"ts_code": "000001.SZ", "time": "2026-08-22 09:32:00"}])
        self.assertEqual(calls, ["prepare", "success"])

    def test_local_capacity_closes_ledger_without_provider_failure(self):
        async def check():
            calls: list[str] = []

            async def saturated(*_: object, **__: object):
                raise ExecutorSaturatedError("super_get executor saturated")

            with self.assertRaises(HTTPException) as caught:
                await fetch_catalog(self.request(), self.dependencies(saturated, calls))
            return caught.exception, calls

        error, calls = asyncio.run(check())
        self.assertEqual(error.status_code, 503)
        self.assertEqual(calls, ["prepare", "blocked"])


if __name__ == "__main__":
    unittest.main()
