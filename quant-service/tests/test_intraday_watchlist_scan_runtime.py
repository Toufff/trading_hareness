"""Regression coverage for the intraday watchlist I/O adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
import uuid

from app.intraday_watchlist_scan_runtime import (
    IntradayWatchlistScanRuntime,
    IntradayWatchlistScanRuntimeDependencies,
)


class IntradayWatchlistScanRuntimeTests(unittest.TestCase):
    def test_runtime_binds_watchlist_reads_terminal_writes_and_duration_once(self) -> None:
        database = object()
        async_database = object()
        calls: list[tuple[str, object]] = []
        durations: list[tuple[str, float]] = []
        clock = iter((10.0, 12.5))

        async def run_database(operation, *args, **kwargs):
            calls.append(("database", kwargs.get("timeout_seconds")))
            return operation(*args)

        async def read_watchlists(received_database, symbols, *, max_symbols):
            self.assertIs(received_database, async_database)
            self.assertEqual(symbols, ["000001.SZ"])
            self.assertEqual(max_symbols, 40)
            return [{"symbol": "000001.SZ"}]

        def terminal(*args):
            calls.append(("terminal", args))

        async def run_scan(request, dependencies):
            self.assertEqual(await dependencies.load_watches(request.symbols), [{"symbol": "000001.SZ"}])
            await dependencies.persist_terminal(
                uuid.UUID(int=1), datetime(2026, 8, 24, tzinfo=timezone.utc), "blocked",
                request.symbols, {"source": "unavailable"}, {"watched": 1},
            )
            return {"status": "blocked"}

        async def empty_async(*_args, **_kwargs):
            return {}

        dependencies = IntradayWatchlistScanRuntimeDependencies(
            clock=lambda: next(clock), observe_duration=lambda status, seconds: durations.append((status, seconds)),
            now_utc=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc), new_scan_id=uuid.uuid4,
            async_database=async_database, database=database, run_database=run_database,
            watchlist_capacity=lambda _: {"max_symbols": 40}, read_watchlists=read_watchlists,
            persist_terminal=terminal, realtime_session=empty_async, prune_rule_inputs=empty_async,
            retry_pending_alerts=empty_async, read_exact_memberships=empty_async,
            mapped_peers=lambda *_: {}, high_frequency_window=lambda _: False,
            quote_capture_dependencies=object(), surge_context=empty_async, peer_context=lambda *_: {},
            watch_priority_key=lambda row: row["symbol"], realtime_validation_slice=lambda symbols, offset, limit: (symbols, offset),
            tushare_minutes=empty_async, fast_confirmations=empty_async, board_cache_evidence=empty_async,
            build_source_status=lambda **_: {}, persist_signals=lambda *_: [], read_shadow_pool=empty_async,
            shadow_rotation_due=lambda _: False, shadow_rotation_slice=lambda *_: ([], 0),
            tencent_watch_quotes=empty_async, merge_watch_prices=lambda *_: None, safe_error=lambda value, _: value,
            shadow_quote_errors=(ValueError,), rotation_persistence_dependencies=object(),
            persist_rotation_observations=lambda **_: {}, persist_rotation_scan_status=lambda *_args, **_kwargs: None,
            json_safe=lambda value: value, deliver_alert=empty_async, alert_text=lambda *_args, **_kwargs: "",
            decision_card_url=lambda _: None, run_scan=run_scan,
        )

        result = asyncio.run(IntradayWatchlistScanRuntime(dependencies).run(
            SimpleNamespace(symbols=["000001.SZ"]),
        ))

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(durations, [("blocked", 2.5)])
        self.assertEqual(calls[0], ("database", None))
        self.assertEqual(calls[1][0], "terminal")


if __name__ == "__main__":
    unittest.main()
