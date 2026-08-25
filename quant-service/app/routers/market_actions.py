"""Market write-action routes with injected orchestration services.

HTTP handlers own only validation and URL compatibility.  The injected service
functions retain their existing bounded fetch, raw-evidence, and persistence
contracts, so importing this module cannot start a market request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..request_models import (
    AnnouncementSyncRequest,
    BarsImport,
    FullMarketDailyControlsSyncRequest,
    FullMarketDailySyncRequest,
    MarketFlowFeatureRebuildRequest,
    MarketUniverseSyncRequest,
    PostCloseRefreshRequest,
)


@dataclass(frozen=True)
class MarketActionDependencies:
    import_bars: Callable[[BarsImport], dict[str, int]]
    sync_universe: Callable[[MarketUniverseSyncRequest], Awaitable[dict[str, Any]]]
    sync_full_daily: Callable[[FullMarketDailySyncRequest], Awaitable[dict[str, Any]]]
    sync_full_daily_controls: Callable[[FullMarketDailyControlsSyncRequest], Awaitable[dict[str, Any]]]
    post_close_refresh: Callable[[PostCloseRefreshRequest], Awaitable[dict[str, Any]]]
    start_post_close_refresh: Callable[[PostCloseRefreshRequest], Awaitable[dict[str, Any]]]
    sync_announcements: Callable[[AnnouncementSyncRequest], Awaitable[dict[str, Any]]]
    rebuild_market_flow_features: Callable[[MarketFlowFeatureRebuildRequest], Awaitable[dict[str, Any]]]


def build_market_actions_router(deps: MarketActionDependencies) -> APIRouter:
    """Build mutating market endpoints without coupling them to ``main``."""
    router = APIRouter(tags=["market-actions"])

    @router.post("/api/v1/market/bars/import")
    def import_bars(payload: BarsImport) -> dict[str, int]:
        return deps.import_bars(payload)

    @router.post("/api/v1/market/universe/sync")
    async def sync_market_universe(payload: MarketUniverseSyncRequest) -> dict[str, Any]:
        return await deps.sync_universe(payload)

    @router.post("/api/v1/market/sync/full-daily")
    async def sync_full_market_daily(payload: FullMarketDailySyncRequest) -> dict[str, Any]:
        return await deps.sync_full_daily(payload)

    @router.post("/api/v1/market/sync/full-daily-controls")
    async def sync_full_market_daily_controls(payload: FullMarketDailyControlsSyncRequest) -> dict[str, Any]:
        return await deps.sync_full_daily_controls(payload)

    @router.post("/api/v1/market/post-close/refresh")
    async def post_close_refresh(payload: PostCloseRefreshRequest) -> dict[str, Any]:
        return await deps.post_close_refresh(payload)

    @router.post("/api/v1/market/post-close/refresh/start")
    async def start_post_close_refresh(payload: PostCloseRefreshRequest) -> dict[str, Any]:
        return await deps.start_post_close_refresh(payload)

    @router.post("/api/v1/events/cninfo/sync")
    async def sync_cninfo_announcements(payload: AnnouncementSyncRequest) -> dict[str, Any]:
        return await deps.sync_announcements(payload)

    @router.post("/api/v1/market/flow/features/rebuild")
    async def rebuild_market_flow_features(payload: MarketFlowFeatureRebuildRequest) -> dict[str, Any]:
        return await deps.rebuild_market_flow_features(payload)

    return router


__all__ = ["MarketActionDependencies", "build_market_actions_router"]
