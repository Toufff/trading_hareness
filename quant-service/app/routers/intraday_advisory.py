"""Read-only intraday advisory observability endpoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Query


def build_intraday_advisory_router(async_database: Any, *, read_status: Callable[..., Any],
                                    runtime_enabled: Callable[[], bool],
                                    transport_configured: Callable[[], bool]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/intraday/advisory", tags=["intraday-advisory"])

    @router.get("/status")
    async def advisory_status(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        payload = await read_status(async_database, limit=limit)
        return {
            "enabled": runtime_enabled(), "transport_configured": transport_configured(),
            "cadence": {"quote_acquisition_seconds": 5, "index_acquisition_seconds": 15,
                        "industry_board_source_seconds": 60, "local_evaluation_seconds": 1,
                        "deepseek_seconds": 600, "codex_seconds": 1800,
                        "special_reports": ["11:35", "14:45"]},
            **payload,
        }

    return router


__all__ = ["build_intraday_advisory_router"]
