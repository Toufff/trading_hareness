"""Bounded market-ingestion action routes.

The router contains no provider implementation and no filesystem/SQL access.
Its injected services retain the application's allow-lists, idempotency ledgers,
raw-first persistence, circuit checks, and local-capacity semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..request_models import MarketSnapshotRequest, OfflineMinuteImportRequest, TushareSyncRequest


@dataclass(frozen=True)
class IngestionActionDependencies:
    market_snapshot: Callable[[MarketSnapshotRequest], Awaitable[dict[str, Any]]]
    import_offline_minutes: Callable[[OfflineMinuteImportRequest], Awaitable[dict[str, Any]]]
    sync_tushare: Callable[[TushareSyncRequest], Awaitable[dict[str, Any]]]
    sync_baostock: Callable[[TushareSyncRequest], Awaitable[dict[str, Any]]]
    sync_tushare_core: Callable[[TushareSyncRequest], Awaitable[dict[str, Any]]]


def build_ingestion_actions_router(deps: IngestionActionDependencies) -> APIRouter:
    """Build stable write routes without constructing clients or workers."""
    router = APIRouter(tags=["ingestion-actions"])

    @router.post("/api/v1/market/snapshots/run")
    async def market_snapshot(payload: MarketSnapshotRequest) -> dict[str, Any]:
        return await deps.market_snapshot(payload)

    @router.post("/api/v1/market/minute/import-offline")
    async def import_offline_minutes(payload: OfflineMinuteImportRequest) -> dict[str, Any]:
        return await deps.import_offline_minutes(payload)

    @router.post("/api/v1/market/sync/tushare")
    async def sync_tushare(payload: TushareSyncRequest) -> dict[str, Any]:
        return await deps.sync_tushare(payload)

    @router.post("/api/v1/market/sync/baostock")
    async def sync_baostock(payload: TushareSyncRequest) -> dict[str, Any]:
        return await deps.sync_baostock(payload)

    @router.post("/api/v1/market/sync/tushare/core")
    async def sync_tushare_core(payload: TushareSyncRequest) -> dict[str, Any]:
        return await deps.sync_tushare_core(payload)

    return router


__all__ = ["IngestionActionDependencies", "build_ingestion_actions_router"]
