"""Read-only routes for materialized market/research results."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import APIRouter

from .. import market_result_read_model as read_model
from .. import async_market_result_read_repository as async_read_model
from ..runtime_executors import run_database_blocking


def build_market_result_reads_router(
    database: Any,
    catalog: Iterable[str],
    current_data_coverage_fn: Callable[..., dict[str, Any]],
    feature_readiness_fn: Callable[..., dict[str, Any]],
    history_estimate_fn: Callable[[], dict[str, Any]],
    offline_root_fn: Callable[[], Path],
    analyst_scorecard_readiness_fn: Callable[..., dict[str, Any]],
    async_database: Any | None = None,
) -> APIRouter:
    router = APIRouter(tags=["market-result-reads"])

    @router.get("/api/v1/providers/tushare/raw")
    async def raw(api_name: str, provider: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return await async_read_model.tushare_raw(async_database, api_name, provider, limit, offset, catalog) if async_database else read_model.tushare_raw(database, api_name, provider, limit, offset, catalog)

    @router.get("/api/v1/research/overview")
    async def overview() -> dict[str, Any]:
        if async_database:
            history_estimate = await run_database_blocking(history_estimate_fn, timeout_seconds=15)
            return await async_read_model.research_overview(async_database, history_estimate)
        return read_model.research_overview(
            database, current_data_coverage_fn=current_data_coverage_fn, feature_readiness_fn=feature_readiness_fn,
            history_estimate_fn=history_estimate_fn,
        )

    @router.get("/api/v1/market/snapshots")
    async def snapshots(limit: int = 20) -> dict[str, Any]:
        return await async_read_model.market_snapshots(async_database, limit) if async_database else read_model.market_snapshots(database, limit)

    @router.get("/api/v1/market/minute/imports")
    async def minute_imports(limit: int = 30) -> dict[str, Any]:
        return await async_read_model.offline_minute_imports(async_database, limit, str(offline_root_fn())) if async_database else read_model.offline_minute_imports(database, limit, str(offline_root_fn()))

    @router.get("/api/v1/analyst-scorecards")
    async def scorecards(limit: int = 200) -> dict[str, Any]:
        if async_database:
            return await async_read_model.analyst_scorecards(async_database, limit)
        return read_model.analyst_scorecards(database, limit, analyst_scorecard_readiness_fn)

    @router.get("/api/v1/recommendations/latest")
    async def recommendations() -> dict[str, Any]:
        return await async_read_model.latest_recommendations(async_database) if async_database else read_model.latest_recommendations(database)

    @router.get("/api/v1/metrics")
    async def metric_counts() -> Any:
        return await async_read_model.metrics(async_database) if async_database else read_model.metrics(database)

    return router
