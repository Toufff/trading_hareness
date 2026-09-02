"""Authenticated read gateway for the licensed Longhu adapter.

Logical quote requests are unrestricted by the gateway.  The router splits
them into physical provider calls of at most 300 symbols and combines the
normalized rows for compatibility with older clients.
"""

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
        symbols: str = Query(..., min_length=6),
        x_quant_read_key: str | None = Header(default=None, alias="X-Quant-Read-Key"),
    ) -> dict[str, Any]:
        authorize(x_quant_read_key)
        requested = list(dict.fromkeys(item.strip().upper() for item in symbols.split(",") if item.strip()))
        if not requested:
            raise HTTPException(status_code=422, detail="at least one symbol is required")
        rows: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        for start in range(0, len(requested), 300):
            page_rows, page_status = await quotes(requested[start:start + 300], 300)
            rows.extend(page_rows)
            statuses.append(page_status)

        source_status = dict(statuses[0]) if len(statuses) == 1 else {
            "status": (
                "completed"
                if all(str(item.get("status") or "").lower() == "completed" for item in statuses)
                else "partial"
            ),
            "physical_calls": len(statuses),
            "requested_symbols": len(requested),
            "pages": statuses,
        }
        return {
            "rows": rows,
            "source_status": source_status,
            "requested_symbols": len(requested),
            "physical_calls": len(statuses),
            "physical_request_limit": 300,
        }

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
