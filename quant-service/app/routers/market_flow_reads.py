"""Read-only API for multiscale volume and market-flow research features."""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_market_flow_read_repository import market_flow_features as async_market_flow_features
from ..market_flow_read_model import market_flow_features


def build_market_flow_reads_router(
    database: Any,
    *,
    async_database: Any | None = None,
    async_features_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["market-flow-reads"])

    @router.get("/api/v1/market/flow/features")
    async def features(trade_date: date | None = None, limit: int = 720) -> dict[str, Any]:
        if async_database is not None:
            return await (async_features_fn or async_market_flow_features)(async_database, trade_date, limit=limit)
        return market_flow_features(database, trade_date, limit=limit)

    return router


__all__ = ["build_market_flow_reads_router"]
