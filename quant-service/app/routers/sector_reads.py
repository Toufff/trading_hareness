"""Read-only routes for persisted THS sector and concept evidence."""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from .. import async_sector_read_repository as async_read_model
from .. import sector_read_model as read_model


def build_sector_reads_router(
    database: Any,
    backfill_enabled_fn: Any,
    backfill_batch_size_fn: Any,
    *,
    async_database: Any | None = None,
    async_backfill_status_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    async_concepts_fn: Callable[[Any, date | None, int], Awaitable[dict[str, Any]]] | None = None,
    async_candidates_fn: Callable[[Any, date | None, int], Awaitable[dict[str, Any]]] | None = None,
    async_flows_fn: Callable[[Any, str, date | None, int], Awaitable[dict[str, Any]]] | None = None,
    async_sectors_fn: Callable[[Any, str, int, int], Awaitable[dict[str, Any]]] | None = None,
    async_members_fn: Callable[[Any, str, str, int, int], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["sector-reads"])

    @router.get("/api/v1/market/sectors/concepts/members/backfill/status")
    async def member_backfill_status(trade_date: date | None = None) -> dict[str, Any]:
        if async_database is not None:
            return await (async_backfill_status_fn or async_read_model.concept_member_backfill_status)(
                async_database, trade_date, automatic_enabled=backfill_enabled_fn(), batch_size=backfill_batch_size_fn(),
            )
        return read_model.concept_member_backfill_status(
            database, trade_date, automatic_enabled=backfill_enabled_fn(), batch_size=backfill_batch_size_fn(),
        )

    @router.get("/api/v1/market/sectors/concepts")
    async def concepts(trade_date: date | None = None, limit: int = 500) -> dict[str, Any]:
        if async_database is not None:
            return await (async_concepts_fn or async_read_model.concept_sector_signals)(async_database, trade_date, limit)
        return read_model.concept_sector_signals(database, trade_date, limit)

    @router.get("/api/v1/market/sectors/concepts/candidates")
    async def concept_candidates(trade_date: date | None = None, limit: int = 100) -> dict[str, Any]:
        if async_database is not None:
            return await (async_candidates_fn or async_read_model.concept_limit_candidates)(async_database, trade_date, limit)
        return read_model.concept_limit_candidates(database, trade_date, limit)

    @router.get("/api/v1/market/sectors/flows")
    async def flows(taxonomy_key: str = "ths_industry", trade_date: date | None = None, limit: int = 100) -> dict[str, Any]:
        if async_database is not None:
            return await (async_flows_fn or async_read_model.sector_flows)(async_database, taxonomy_key, trade_date, limit)
        return read_model.sector_flows(database, taxonomy_key, trade_date, limit)

    @router.get("/api/v1/market/sectors")
    async def sectors(taxonomy_key: str = "ths_index_n", limit: int = 500, offset: int = 0) -> dict[str, Any]:
        if async_database is not None:
            return await (async_sectors_fn or async_read_model.market_sectors)(async_database, taxonomy_key, limit, offset)
        return read_model.market_sectors(database, taxonomy_key, limit, offset)

    @router.get("/api/v1/market/sectors/{sector_key}/members")
    async def members(sector_key: str, taxonomy_key: str = "ths_index_n", limit: int = 500, offset: int = 0) -> dict[str, Any]:
        if async_database is not None:
            return await (async_members_fn or async_read_model.sector_members)(async_database, sector_key, taxonomy_key, limit, offset)
        return read_model.sector_members(database, sector_key, taxonomy_key, limit, offset)

    return router
