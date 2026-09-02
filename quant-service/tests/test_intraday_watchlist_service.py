from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from app.intraday_watchlist_service import (
    IntradayWatchlistDependencies,
    WatchlistHistoryHydrationDependencies,
    hydrate_watchlist_history,
    sync_history,
    upsert,
)


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


def _hydration_deps(**overrides) -> WatchlistHistoryHydrationDependencies:
    database = MagicMock()
    connection = MagicMock()
    database.transaction.return_value.__enter__.return_value = connection

    async def run_database(action, *args):
        return action(*args) if args else action()

    values = {
        "database": database,
        "run_database": run_database,
        "sync_tushare": AsyncMock(return_value={"status": "completed"}),
        "fetch_supplemental": AsyncMock(side_effect=lambda label, _request: ({"source": label, "status": "completed"}, [])),
        "daily_factors": lambda _symbol: {"bar_count": 30},
        "json_safe": lambda value: value,
    }
    values.update(overrides)
    return WatchlistHistoryHydrationDependencies(**values)


class HydrateWatchlistHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_status_requires_daily_bars_and_two_supplemental_sources(self) -> None:
        deps = _hydration_deps()

        result = await hydrate_watchlist_history(uuid.uuid4(), "600176.SH", deps)

        self.assertEqual(result["status"], "completed")
        self.assertIn("factors", result)
        self.assertTrue(result["factors"]["factor_ready"])

    async def test_partial_status_when_daily_ok_but_supplemental_sources_are_thin(self) -> None:
        deps = _hydration_deps(
            fetch_supplemental=AsyncMock(side_effect=lambda label, _request: ({"source": label, "status": "failed"}, [])),
        )

        result = await hydrate_watchlist_history(uuid.uuid4(), "600176.SH", deps)

        self.assertEqual(result["status"], "partial")

    async def test_failed_status_when_daily_bar_count_is_too_low(self) -> None:
        deps = _hydration_deps(daily_factors=lambda _symbol: {"bar_count": 5})

        result = await hydrate_watchlist_history(uuid.uuid4(), "600176.SH", deps)

        self.assertEqual(result["status"], "failed")
