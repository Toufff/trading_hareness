"""Read-only routes for stored research catalog and experiment evidence."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import research_catalog_read_model as read_model
from .. import async_research_catalog_read_repository as async_read_model


def build_research_catalog_reads_router(database: Any, async_database: Any | None = None) -> APIRouter:
    router = APIRouter(tags=["research-catalog-reads"])

    @router.get("/api/v1/universes/{universe_key}")
    async def universe(universe_key: str) -> dict[str, Any]:
        return await async_read_model.universe_members(async_database, universe_key) if async_database else read_model.universe_members(database, universe_key)

    @router.get("/api/v1/features/latest")
    async def features(universe_key: str = "core", limit: int = 200) -> dict[str, Any]:
        return await async_read_model.latest_features(async_database, universe_key, limit) if async_database else read_model.latest_features(database, universe_key, limit)

    @router.get("/api/v1/factors")
    async def factors() -> dict[str, Any]:
        return await async_read_model.factor_registry(async_database) if async_database else read_model.factor_registry(database)

    @router.get("/api/v1/factors/evaluations")
    async def factor_evaluation_history(universe_key: str = "core", limit: int = 100) -> dict[str, Any]:
        return await async_read_model.factor_evaluations(async_database, universe_key, limit) if async_database else read_model.factor_evaluations(database, universe_key, limit)

    @router.get("/api/v1/strategies")
    async def strategies() -> dict[str, Any]:
        return await async_read_model.strategy_registry(async_database) if async_database else read_model.strategy_registry(database)

    @router.get("/api/v1/strategies/experiments")
    async def experiments(universe_key: str = "core", limit: int = 50) -> dict[str, Any]:
        return await async_read_model.strategy_experiments(async_database, universe_key, limit) if async_database else read_model.strategy_experiments(database, universe_key, limit)

    @router.get("/api/v1/data-quality/issues")
    async def quality_issues(limit: int = 100) -> dict[str, Any]:
        return await async_read_model.data_quality_issues(async_database, limit) if async_database else read_model.data_quality_issues(database, limit)

    return router
