"""Read-only routes for stored intraday evidence."""

from __future__ import annotations

import re
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from ..async_intraday_evidence_read_repository import latest_scan as async_latest_scan
from ..async_intraday_evidence_read_repository import watchlists as async_watchlists
from .. import intraday_evidence_read_model as read_model


def build_intraday_evidence_reads_router(
    database: Any,
    decision_card_fn: Callable[[Any, str], dict[str, Any]],
    *,
    async_database: Any | None = None,
) -> APIRouter:
    router = APIRouter(tags=["intraday-evidence-reads"])

    @router.get("/api/v1/intraday/watchlists")
    async def list_watchlists() -> dict[str, Any]:
        if async_database is not None:
            return await async_watchlists(async_database)
        return read_model.watchlists(database)

    @router.get("/api/v1/intraday/decision-cards/{symbol}")
    def latest_decision_card(symbol: str) -> dict[str, Any]:
        normalized = symbol.upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", normalized):
            raise HTTPException(status_code=422, detail="symbol must use the Tushare form, for example 600176.SH")
        return read_model.decision_card(database, normalized, decision_card_fn)

    @router.get("/api/v1/intraday/scans/latest")
    async def latest_intraday_scan(limit: int = 100) -> dict[str, Any]:
        if async_database is not None:
            return await async_latest_scan(async_database, limit=limit)
        return read_model.latest_scan(database, limit=limit)

    return router
