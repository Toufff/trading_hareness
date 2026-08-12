"""Mutating provider routes with explicit service dependencies.

The handlers remain thin: request validation is defined in ``request_models``
and all provider/network work stays in the injected services.  This makes the
HTTP boundary independently inspectable without allowing this router to own a
client, credential, or application singleton.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from ..request_models import (
    AkShareProbeRequest,
    RealtimeProbeRequest,
    StockStudyRequest,
    TushareCapabilityAuditRequest,
    TushareFetchRequest,
)


@dataclass(frozen=True)
class ProviderActionDependencies:
    akshare_probe: Callable[[AkShareProbeRequest], Awaitable[dict[str, Any]]]
    realtime_probe: Callable[[RealtimeProbeRequest], Awaitable[dict[str, Any]]]
    tushare_audit: Callable[[TushareCapabilityAuditRequest], Awaitable[dict[str, Any]]]
    tushare_fetch: Callable[[TushareFetchRequest], Awaitable[dict[str, Any]]]
    stock_study: Callable[[str, StockStudyRequest], Awaitable[dict[str, Any]]]


def build_provider_actions_router(deps: ProviderActionDependencies) -> APIRouter:
    """Expose bounded provider actions while preserving the existing URLs."""
    router = APIRouter(tags=["provider-actions"])

    @router.post("/api/v1/providers/akshare/probe")
    async def akshare_probe(payload: AkShareProbeRequest) -> dict[str, Any]:
        return await deps.akshare_probe(payload)

    @router.post("/api/v1/providers/realtime/probe")
    async def realtime_probe(payload: RealtimeProbeRequest) -> dict[str, Any]:
        return await deps.realtime_probe(payload)

    @router.post("/api/v1/providers/tushare/audit")
    async def tushare_audit(payload: TushareCapabilityAuditRequest) -> dict[str, Any]:
        return await deps.tushare_audit(payload)

    @router.post("/api/v1/providers/tushare/fetch")
    async def tushare_fetch(payload: TushareFetchRequest) -> dict[str, Any]:
        return await deps.tushare_fetch(payload)

    @router.post("/api/v1/stocks/{symbol}/study")
    async def stock_study(symbol: str, payload: StockStudyRequest | None = None) -> dict[str, Any]:
        normalized = symbol.upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", normalized):
            raise HTTPException(status_code=422, detail="symbol must use the Tushare form, for example 000636.SZ")
        return await deps.stock_study(normalized, payload or StockStudyRequest())

    return router


__all__ = ["ProviderActionDependencies", "build_provider_actions_router"]
