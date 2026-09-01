"""Authenticated, bounded read gateway for the licensed Longhu adapter."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query


def build_longhu_reads_router(
    *,
    configured: Callable[[], bool],
    shared_read_key: Callable[[], str],
    quotes: Callable[[list[str], int], Awaitable[tuple[list[dict[str, Any]], dict[str, Any]]]],
    minutes: Callable[[str], Awaitable[list[dict[str, Any]]]],
) -> APIRouter:
    """Expose licensed evidence without distributing the upstream token."""
    router = APIRouter(prefix="/licensed/longhu", tags=["licensed-longhu"])

    def authorize(supplied: str | None) -> None:
        expected = shared_read_key().strip()
        if not expected:
            raise HTTPException(status_code=503, detail="shared licensed read gateway is disabled")
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="valid X-Quant-Read-Key is required")
        if not configured():
            raise HTTPException(status_code=503, detail="Longhu provider is not configured")

    @router.get("/quotes")
    async def read_quotes(
        symbols: str = Query(..., min_length=6, max_length=4_000),
        x_quant_read_key: str | None = Header(default=None, alias="X-Quant-Read-Key"),
    ) -> dict[str, Any]:
        authorize(x_quant_read_key)
        requested = list(dict.fromkeys(item.strip().upper() for item in symbols.split(",") if item.strip()))
        if not requested:
            raise HTTPException(status_code=422, detail="at least one symbol is required")
        if len(requested) > 300:
            raise HTTPException(status_code=422, detail="one logical gateway request is capped at 300 symbols")
        rows, status = await quotes(requested, 300)
        return {"rows": rows, "source_status": status, "physical_request_limit": 300}

    @router.get("/minutes/{symbol}")
    async def read_minutes(
        symbol: str,
        x_quant_read_key: str | None = Header(default=None, alias="X-Quant-Read-Key"),
    ) -> dict[str, Any]:
        authorize(x_quant_read_key)
        rows = await minutes(symbol.upper())
        return {
            "symbol": symbol.upper(), "rows": rows,
            "source": "longhuvip:GetStockTrendIncremental", "physical_request_limit": 300,
        }

    return router


__all__ = ["build_longhu_reads_router"]
