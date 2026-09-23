"""Read-only intraday advisory observability endpoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from datetime import datetime, timezone
from fastapi import APIRouter, Query
from ..intraday_advisory.notice_policy import cadence_status
from ..intraday_advisory.focus import list_focus, set_focus, clear_focus


def build_intraday_advisory_router(async_database: Any, *, read_status: Callable[..., Any],
                                    runtime_enabled: Callable[[], bool],
                                    transport_configured: Callable[[], bool]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/intraday/advisory", tags=["intraday-advisory"])

    @router.get("/status")
    async def advisory_status(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        payload = await read_status(async_database, limit=limit)
        return {
            "enabled": runtime_enabled(), "transport_configured": transport_configured(),
            "cadence": cadence_status(),
            **payload,
        }

    @router.get('/focus')
    async def focus_list(account_key: str = Query('citics-primary')) -> dict[str, Any]:
        return await list_focus(async_database, account_key, datetime.now(timezone.utc))

    @router.put('/focus/{symbol}')
    async def focus_set(symbol: str, account_key: str = Query('citics-primary')) -> dict[str, Any]:
        return await set_focus(async_database, account_key, symbol, datetime.now(timezone.utc))

    @router.delete('/focus/{symbol}')
    async def focus_clear(symbol: str, account_key: str = Query('citics-primary')) -> dict[str, Any]:
        return await clear_focus(async_database, account_key, symbol)

    return router


__all__ = ["build_intraday_advisory_router"]
