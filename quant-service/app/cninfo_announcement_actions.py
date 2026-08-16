"""Bounded CNInfo announcement synchronization actions.

The API wrapper keeps its existing dependency seams so provider circuit tests
and the live service both execute the same, small orchestration path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .provider_health import record_provider_failure, record_provider_success


class CninfoAnnouncementActions:
    """Own the bounded, persisted CNInfo synchronization flow."""

    def __init__(self, database: Any) -> None:
        self._database = database

    def symbols(self, request: Any) -> list[str]:
        if request.symbols:
            return list(request.symbols)
        with self._database.transaction() as connection:
            rows = connection.execute(
                """SELECT symbol FROM quant.universe_members
                   WHERE universe_key=%s AND enabled ORDER BY priority,symbol LIMIT 50""",
                (request.universe_key,),
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def persist_provider_health(
        self, status: str, stored: int, failures: list[str], latency_ms: int | None = None,
    ) -> None:
        with self._database.transaction() as connection:
            if status == "failed":
                record_provider_failure(connection, "cninfo_free", "announcement", " | ".join(failures), latency_ms)
            else:
                record_provider_success(connection, "cninfo_free", "announcement", stored, latency_ms)
                if failures:
                    record_provider_failure(connection, "cninfo_free", "announcement", " | ".join(failures), latency_ms)

    async def sync(
        self,
        request: Any,
        *,
        run_database: Callable[..., Awaitable[Any]],
        provider_capabilities: Callable[[str, list[str]], Awaitable[set[str]]],
        symbols: Callable[[Any], list[str]],
        fetch_announcements: Callable[..., Awaitable[list[dict[str, Any]]]],
        persist_events: Callable[[str, list[dict[str, Any]]], int],
        persist_health: Callable[[str, int, list[str], int | None], None],
    ) -> dict[str, Any]:
        """Read only bounded provider windows and persist each symbol independently."""
        end = request.end_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        start = request.start_date or end - timedelta(days=request.lookback_days)
        selected_symbols = request.symbols or await run_database(symbols, request)
        if not selected_symbols:
            return {
                "status": "blocked", "reason": "no symbols supplied and universe is empty",
                "provider": "cninfo_free",
            }
        request_key = hashlib.sha256(json.dumps({
            "provider": "cninfo_free", "symbols": selected_symbols, "start": str(start), "end": str(end),
            "pages": request.max_pages_per_symbol,
        }, sort_keys=True).encode()).hexdigest()
        if "announcement" in await provider_capabilities("cninfo_free", ["announcement"]):
            return {
                "status": "blocked", "reason": "provider health circuit is open; upstream request skipped",
                "provider": "cninfo_free", "request_key": request_key, "symbols": selected_symbols,
                "start_date": str(start), "end_date": str(end), "received": 0, "stored": 0,
                "failures": [], "decision_eligible": False,
            }
        started_at = asyncio.get_running_loop().time()
        stored = received = 0
        failures: list[str] = []
        for symbol in selected_symbols:
            try:
                rows = await fetch_announcements(symbol, start, end, max_pages=request.max_pages_per_symbol)
                received += len(rows)
                stored += await run_database(persist_events, "cninfo_free", rows, timeout_seconds=60)
            except Exception as error:  # noqa: BLE001 - remaining symbols remain useful evidence
                failures.append(f"{symbol}: {str(error)[:180]}")
        status = "completed" if not failures else "partial" if stored or received else "failed"
        await run_database(
            persist_health, status, stored, failures,
            round((asyncio.get_running_loop().time() - started_at) * 1000),
        )
        return {
            "status": status, "provider": "cninfo_free", "request_key": request_key, "symbols": selected_symbols,
            "start_date": str(start), "end_date": str(end), "received": received, "stored": stored,
            "failures": failures, "decision_eligible": False,
        }
