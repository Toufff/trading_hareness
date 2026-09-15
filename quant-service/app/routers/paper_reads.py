"""Read-only paper research ledger projections."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..async_paper_read_repository import paper_status, strategy_contracts, strategy_funnel
from ..paper_read_model import paper_status as sync_paper_status, strategy_contracts as sync_strategy_contracts, strategy_funnel as sync_strategy_funnel


def build_paper_reads_router(database: Any, async_database: Any | None = None) -> APIRouter:
    router = APIRouter(tags=["paper-research"])

    @router.get("/api/v1/paper/status")
    async def status(limit: int = 50) -> dict[str, Any]:
        return await paper_status(async_database, limit) if async_database else sync_paper_status(database, limit)

    @router.get("/api/v1/strategy/funnel")
    async def funnel(limit: int = 100) -> dict[str, Any]:
        return await strategy_funnel(async_database, limit) if async_database else sync_strategy_funnel(database, limit)

    @router.get("/api/v1/strategy/contracts")
    async def contracts() -> dict[str, Any]:
        return await strategy_contracts(async_database) if async_database else sync_strategy_contracts(database)

    return router


__all__ = ["build_paper_reads_router"]
