"""Authenticated read gateway for the licensed Longhu adapter.

Logical quote requests are unrestricted by the gateway.  The router splits
them into physical provider calls of at most 300 symbols and combines the
normalized rows for compatibility with older clients.
"""

from __future__ import annotations

import secrets
import gzip
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ..runtime_executors import ExecutorSaturatedError


class MinuteBatchResponse(BaseModel):
    rows: dict[str, list[dict[str, Any]]]
    errors: dict[str, str]
    requested: int
    completed: int
    deadline_seconds: float
    session_guard: str
    source: str
    physical_request_limit: int


def _gzip_accepted(value: str) -> bool:
    for item in value.lower().split(","):
        coding, *parameters = item.strip().split(";")
        if coding == "gzip":
            try:
                return all(float(part.strip()[2:]) > 0 for part in parameters if part.strip().startswith("q="))
            except ValueError:
                return False
    return False


def _minute_response(payload: dict[str, Any], accept_encoding: str) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    headers = {"Vary": "Accept-Encoding", "Cache-Control": "no-store"}
    if len(body) >= 64 * 1024 and _gzip_accepted(accept_encoding):
        body = gzip.compress(body, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/json", headers=headers)


def build_longhu_reads_router(
    *,
    configured: Callable[[], bool],
    shared_read_key: Callable[[], str],
    quotes: Callable[[list[str], int], Awaitable[tuple[list[dict[str, Any]], dict[str, Any]]]],
    minutes: Callable[[str], Awaitable[list[dict[str, Any]]]],
    minutes_batch: Callable[[list[str], float], Awaitable[dict[str, Any]]] | None = None,
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

    if minutes_batch is not None:
        @router.get("/minutes", response_model=MinuteBatchResponse)
        async def read_minutes_batch(
            request: Request,
            symbols: str = Query(..., min_length=6, max_length=4_000),
            deadline_seconds: float = Query(5.5, ge=1.0, le=20.0, allow_inf_nan=False),
            x_quant_read_key: str | None = Header(default=None, alias="X-Quant-Read-Key"),
        ) -> Response:
            authorize(x_quant_read_key)
            requested = list(dict.fromkeys(item.strip().upper() for item in symbols.split(",") if item.strip()))
            if not requested or len(requested) > 300:
                raise HTTPException(422, "one minute basket requires 1..300 unique symbols")
            if any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", item) for item in requested):
                raise HTTPException(422, "minute symbols must use 6 digits plus .SH, .SZ or .BJ")
            try:
                batch = await minutes_batch(requested, deadline_seconds)
            except ExecutorSaturatedError:
                raise HTTPException(503, "minute batch capacity is temporarily saturated", headers={"Retry-After": "1"}) from None
            except TimeoutError:
                raise HTTPException(504, "minute batch gateway deadline exceeded") from None
            except Exception:
                raise HTTPException(502, "minute batch provider unavailable") from None
            rows = {symbol: batch[symbol] for symbol in requested if isinstance(batch.get(symbol), list)}
            errors = {symbol: str(batch.get(symbol) or "minute_batch_missing_symbol") for symbol in requested if symbol not in rows}
            return _minute_response(dict(
                rows=rows, errors=errors, requested=len(requested), completed=len(rows),
                deadline_seconds=deadline_seconds, session_guard="current_exchange_session",
                source="longhuvip:GetStockTrendIncremental", physical_request_limit=300,
            ), request.headers.get("accept-encoding", ""))

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
