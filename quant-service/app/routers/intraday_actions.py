"""Mutating intraday routes with explicit orchestration dependencies.

The router deliberately owns no provider client, scheduler, or database
singleton.  Each injected operation retains the existing bounded execution,
trading-calendar gate, durable alert, and persistence semantics in the service
layer.  Keeping this HTTP assembly separate makes the opening-time control
surface inspectable without changing a strategy rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..request_models import (
    IntradayScanRequest,
    IntradayWatchlistRequest,
    MinuteSessionCaptureRequest,
)


@dataclass(frozen=True)
class IntradayActionDependencies:
    """Existing services needed by the intraday write boundary."""

    upsert_watchlist: Callable[[str, IntradayWatchlistRequest], Awaitable[dict[str, Any]]]
    sync_watchlist_history: Callable[[str], Awaitable[dict[str, Any]]]
    delete_watchlist: Callable[[str], Awaitable[dict[str, Any]]]
    scan_watchlist: Callable[[IntradayScanRequest], Awaitable[dict[str, Any]]]
    capture_minute_sessions: Callable[[MinuteSessionCaptureRequest], Awaitable[dict[str, Any]]]
    board_report: Callable[[], Awaitable[dict[str, Any]]]
    close_board_report: Callable[[], Awaitable[dict[str, Any]]]


def build_intraday_actions_router(deps: IntradayActionDependencies) -> APIRouter:
    """Build URL-compatible intraday mutations without application globals."""
    router = APIRouter(tags=["intraday-actions"])

    @router.put("/api/v1/intraday/watchlists/{symbol}")
    async def upsert_watchlist(symbol: str, payload: IntradayWatchlistRequest) -> dict[str, Any]:
        return await deps.upsert_watchlist(symbol, payload)

    @router.post("/api/v1/intraday/watchlists/{symbol}/history/sync")
    async def sync_watchlist_history(symbol: str) -> dict[str, Any]:
        return await deps.sync_watchlist_history(symbol)

    @router.delete("/api/v1/intraday/watchlists/{symbol}")
    async def delete_watchlist(symbol: str) -> dict[str, Any]:
        return await deps.delete_watchlist(symbol)

    @router.post("/api/v1/intraday/scan")
    async def scan_watchlist(payload: IntradayScanRequest) -> dict[str, Any]:
        return await deps.scan_watchlist(payload)

    @router.post("/api/v1/intraday/minute-sessions/capture")
    async def capture_minute_sessions(payload: MinuteSessionCaptureRequest) -> dict[str, Any]:
        return await deps.capture_minute_sessions(payload)

    @router.post("/api/v1/intraday/board-report/run")
    async def board_report() -> dict[str, Any]:
        return await deps.board_report()

    @router.post("/api/v1/market/sectors/review/report/run")
    async def close_board_report() -> dict[str, Any]:
        return await deps.close_board_report()

    return router


__all__ = ["IntradayActionDependencies", "build_intraday_actions_router"]
