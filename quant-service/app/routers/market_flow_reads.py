"""Read-only API for multiscale volume and market-flow research features."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter

from ..market_flow_read_model import market_flow_features


def build_market_flow_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["market-flow-reads"])

    @router.get("/api/v1/market/flow/features")
    def features(trade_date: date | None = None, limit: int = 720) -> dict[str, Any]:
        return market_flow_features(database, trade_date, limit=limit)

    return router


__all__ = ["build_market_flow_reads_router"]
