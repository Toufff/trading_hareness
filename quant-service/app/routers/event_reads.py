"""Read-only local announcement and LHB evidence routes."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter

from ..event_read_model import market_announcements, market_lhb_events
from ..async_event_read_repository import market_announcements as async_market_announcements, market_lhb_events as async_market_lhb_events


def build_event_reads_router(database: Any, async_database: Any | None = None) -> APIRouter:
    router = APIRouter(tags=["event-reads"])

    @router.get("/api/v1/events/announcements")
    async def announcements(symbol: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return await async_market_announcements(async_database, symbol, limit, offset) if async_database else market_announcements(database, symbol, limit, offset)

    @router.get("/api/v1/events/lhb")
    async def lhb_events(trade_date: date | None = None, limit: int = 100) -> dict[str, Any]:
        return await async_market_lhb_events(async_database, trade_date, limit) if async_database else market_lhb_events(database, trade_date, limit)

    return router
