"""Read-only HTTP projections for manually imported broker order history."""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query


def build_broker_order_history_router(
    async_database: Any,
    summary_fn: Callable[[Any, str], Awaitable[dict[str, Any]]],
    timeline_fn: Callable[..., Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter(tags=["broker-order-history"])

    @router.get("/api/v1/personal/order-history/summary")
    async def summary(account_key: str = Query(min_length=1, max_length=80)) -> dict[str, Any]:
        return await summary_fn(async_database, account_key)

    @router.get("/api/v1/personal/order-history/timeline")
    async def timeline(
        account_key: str = Query(min_length=1, max_length=80),
        symbol: str = Query(pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
        start_date: date = Query(),
        end_date: date = Query(),
        limit: int = Query(default=1500, ge=60, le=3000),
    ) -> dict[str, Any]:
        try:
            return await timeline_fn(
                async_database, account_key=account_key, symbol=symbol,
                start_date=start_date, end_date=end_date, limit=limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    return router


__all__ = ["build_broker_order_history_router"]
