"""Read-only intraday advisory observability endpoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Query
from ..intraday_advisory.notice_policy import cadence_status


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

    return router


__all__ = ["build_intraday_advisory_router"]
