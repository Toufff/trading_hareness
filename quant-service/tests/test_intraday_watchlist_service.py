from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from app.intraday_watchlist_service import IntradayWatchlistDependencies, sync_history, upsert


def _deps(**overrides) -> IntradayWatchlistDependencies:
    values = {
        "database": MagicMock(),
        "run_database": AsyncMock(),
        "hydrate_history": AsyncMock(),
        "exchange_for": lambda _symbol: "SZ",
        "json_value": lambda value: value,
        "http_exception": HTTPException,
    }
    values.update(overrides)
    return IntradayWatchlistDependencies(**values)


class IntradayWatchlistServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_rejects_path_payload_symbol_mismatch_without_writing(self) -> None:
        deps = _deps()
        payload = SimpleNamespace(symbol="600000.SH")

        with self.assertRaises(HTTPException) as raised:
            await upsert("000001.SZ", payload, deps)

        self.assertEqual(raised.exception.status_code, 422)
        deps.run_database.assert_not_awaited()
        deps.hydrate_history.assert_not_awaited()

    async def test_history_sync_rejects_missing_watchlist_without_hydration(self) -> None:
        deps = _deps(run_database=AsyncMock(return_value=None))

        with self.assertRaises(HTTPException) as raised:
            await sync_history("000001.SZ", deps)

        self.assertEqual(raised.exception.status_code, 404)
        deps.hydrate_history.assert_not_awaited()
