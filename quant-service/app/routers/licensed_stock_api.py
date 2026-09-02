"""Authenticated full stock-data proxy.

This router deliberately does not restrict documented operation names.  It
restricts only upstream hosts, injects owner-held credentials, and delegates
physical 300-row batching to the provider adapter.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ..licensed_stock_api import UpstreamStockApiError, catalog


class BatchValues(BaseModel):
    param: str = Field(min_length=1)
    # Matches the upstream vendor's own physical page-size cap: a caller
    # cannot submit more raw batch values than a single provider page would
    # ever legitimately need for one field of this request.
    values: list[Any] = Field(default_factory=list, max_length=300)
    separator: str = ","


class StockApiCall(BaseModel):
    target: str = Field(min_length=1)
    path: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    batch: BatchValues | None = None


def build_licensed_stock_api_router(
    *,
    configured: Callable[[], bool],
    shared_read_key: Callable[[], str],
    call: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter(prefix="/licensed/stock-api", tags=["licensed-stock-api"])

    def authorize(supplied: str | None) -> None:
        expected = shared_read_key().strip()
        if not expected:
            raise HTTPException(status_code=503, detail="shared licensed read gateway is disabled")
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="valid X-Quant-Read-Key is required")
        if not configured():
            raise HTTPException(status_code=503, detail="Longhu provider is not configured")

    @router.get("/catalog")
    async def read_catalog(
        x_quant_read_key: str | None = Header(default=None, alias="X-Quant-Read-Key"),
    ) -> dict[str, Any]:
        authorize(x_quant_read_key)
        return catalog()

    @router.post("/call")
    async def invoke(
        request: StockApiCall,
        x_quant_read_key: str | None = Header(default=None, alias="X-Quant-Read-Key"),
    ) -> dict[str, Any]:
        authorize(x_quant_read_key)
        try:
            return await call(request.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except UpstreamStockApiError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    return router


__all__ = [
    "BatchValues",
    "StockApiCall",
    "build_licensed_stock_api_router",
]
