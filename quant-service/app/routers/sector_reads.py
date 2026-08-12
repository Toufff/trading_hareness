"""Read-only routes for persisted THS sector and concept evidence."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter

from .. import sector_read_model as read_model


def build_sector_reads_router(database: Any, backfill_enabled_fn: Any, backfill_batch_size_fn: Any) -> APIRouter:
    router = APIRouter(tags=["sector-reads"])

    @router.get("/api/v1/market/sectors/concepts/members/backfill/status")
    def member_backfill_status(trade_date: date | None = None) -> dict[str, Any]:
        return read_model.concept_member_backfill_status(
            database, trade_date, automatic_enabled=backfill_enabled_fn(), batch_size=backfill_batch_size_fn(),
        )

    @router.get("/api/v1/market/sectors/concepts")
    def concepts(trade_date: date | None = None, limit: int = 500) -> dict[str, Any]:
        return read_model.concept_sector_signals(database, trade_date, limit)

    @router.get("/api/v1/market/sectors/concepts/candidates")
    def concept_candidates(trade_date: date | None = None, limit: int = 100) -> dict[str, Any]:
        return read_model.concept_limit_candidates(database, trade_date, limit)

    @router.get("/api/v1/market/sectors/flows")
    def flows(taxonomy_key: str = "ths_industry", trade_date: date | None = None, limit: int = 100) -> dict[str, Any]:
        return read_model.sector_flows(database, taxonomy_key, trade_date, limit)

    @router.get("/api/v1/market/sectors")
    def sectors(taxonomy_key: str = "ths_index_n", limit: int = 500, offset: int = 0) -> dict[str, Any]:
        return read_model.market_sectors(database, taxonomy_key, limit, offset)

    @router.get("/api/v1/market/sectors/{sector_key}/members")
    def members(sector_key: str, taxonomy_key: str = "ths_index_n", limit: int = 500, offset: int = 0) -> dict[str, Any]:
        return read_model.sector_members(database, sector_key, taxonomy_key, limit, offset)

    return router
