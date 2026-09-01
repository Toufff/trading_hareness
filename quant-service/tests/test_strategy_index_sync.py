from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.market_snapshot_actions import snapshot_fresh_after
from app.strategy_index_sync import sync_index_context


class StrategyIndexSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_longhu_mode_uses_persisted_public_index_fallback(self) -> None:
        fetch_primary = AsyncMock(side_effect=AssertionError("primary must be bypassed"))
        fetch_public = AsyncMock(return_value=[{"ts_code": "000300.SH", "trade_date": "20260901"}])

        async def run_database(action, *args, **_kwargs):
            return action(*args)

        persist = MagicMock(return_value=1)
        result = await sync_index_context(
            date(2026, 9, 1), ("000300.SH",), prefer_public=True,
            primary_request=MagicMock(), fetch_primary=fetch_primary,
            fetch_public=fetch_public, persist_public=persist, run_database=run_database,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["symbols"]["000300.SH"]["provider"], "eastmoney_free")
        persist.assert_called_once()
        fetch_primary.assert_not_awaited()

    async def test_failed_primary_falls_back_per_symbol(self) -> None:
        async def run_database(action, *args, **_kwargs):
            return action(*args)

        result = await sync_index_context(
            date(2026, 9, 1), ("000300.SH",), prefer_public=False,
            primary_request=lambda *_args: object(),
            fetch_primary=AsyncMock(side_effect=RuntimeError("not configured")),
            fetch_public=AsyncMock(return_value=[{"ts_code": "000300.SH", "trade_date": "20260901"}]),
            persist_public=MagicMock(return_value=1), run_database=run_database,
        )
        self.assertEqual(result["symbols"]["000300.SH"]["status"], "completed")
        self.assertIn("not configured", result["symbols"]["000300.SH"]["primary_error"])

    async def test_failed_eastmoney_uses_tencent_index_fallback(self) -> None:
        async def run_database(action, *args, **_kwargs):
            return action(*args)

        persist = MagicMock(return_value=1)
        result = await sync_index_context(
            date(2026, 9, 1), ("000300.SH",), prefer_public=True,
            primary_request=MagicMock(), fetch_primary=AsyncMock(),
            fetch_public=AsyncMock(side_effect=RuntimeError("eastmoney disconnected")),
            persist_public=persist, run_database=run_database,
            fetch_secondary=AsyncMock(return_value=[{"ts_code": "000300.SH", "trade_date": "20260901"}]),
        )
        item = result["symbols"]["000300.SH"]
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["provider"], "tencent_index_free")
        self.assertIn("eastmoney disconnected", item["eastmoney_error"])


class MarketSnapshotFreshnessTests(unittest.TestCase):
    def test_close_retry_keeps_same_date_post_close_evidence(self) -> None:
        observed = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        cutoff = snapshot_fresh_after(SimpleNamespace(session="close"), observed, date(2026, 9, 1))
        self.assertEqual(cutoff, datetime(2026, 9, 1, 6, 50, tzinfo=timezone.utc))

    def test_midday_keeps_ten_minute_freshness(self) -> None:
        observed = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)
        cutoff = snapshot_fresh_after(SimpleNamespace(session="midday"), observed, date(2026, 9, 1))
        self.assertEqual(cutoff, datetime(2026, 9, 1, 4, 20, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
