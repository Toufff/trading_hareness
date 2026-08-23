from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from app.stock_study_public_service import StockStudyPublicDependencies, fetch


class StockStudyPublicServiceTests(unittest.TestCase):
    def dependencies(self, *, capabilities: set[str] | None = None) -> StockStudyPublicDependencies:
        return StockStudyPublicDependencies(
            open_provider_capabilities=AsyncMock(return_value=capabilities or set()),
            run_database=AsyncMock(side_effect=lambda action, *args, **_kwargs: action(*args)),
            persist_success=Mock(return_value=1),
            persist_failure=Mock(),
            safe_error_detail=lambda value, _limit: value,
            request_errors=(ValueError,),
        )

    def test_circuit_open_does_not_create_the_public_fetch_coroutine(self) -> None:
        dependencies = self.dependencies(capabilities={"daily_bar"})
        fetcher = Mock()

        source, payload = asyncio.run(fetch("腾讯", "tencent_free", "daily_bar", fetcher, "000001.SZ", dependencies))

        self.assertEqual(source["status"], "circuit_open")
        self.assertEqual(payload, [])
        fetcher.assert_not_called()
        dependencies.run_database.assert_not_awaited()

    def test_completed_fetch_records_latency_and_payload_in_the_bounded_database_executor(self) -> None:
        dependencies = self.dependencies()

        async def fetcher() -> list[dict[str, object]]:
            return [{"trade_date": "20260821", "close": 10.0}]

        source, payload = asyncio.run(fetch("腾讯", "tencent_free", "daily_bar", fetcher, "000001.SZ", dependencies))

        self.assertEqual(source["status"], "completed")
        self.assertEqual(payload[0]["close"], 10.0)
        self.assertEqual(dependencies.run_database.await_args.args[0], dependencies.persist_success)
        self.assertGreaterEqual(dependencies.run_database.await_args.args[-1], 0)
