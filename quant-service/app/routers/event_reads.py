"""Read-only local announcement and LHB evidence routes."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter

from ..event_read_model import market_announcements, market_lhb_events


def build_event_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["event-reads"])

    @router.get("/api/v1/events/announcements")
    def announcements(symbol: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return market_announcements(database, symbol, limit, offset)

    @router.get("/api/v1/events/lhb")
    def lhb_events(trade_date: date | None = None, limit: int = 100) -> dict[str, Any]:
        return market_lhb_events(database, trade_date, limit)

    return router
