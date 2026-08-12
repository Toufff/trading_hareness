"""Read-only routes for stored research catalog and experiment evidence."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import research_catalog_read_model as read_model


def build_research_catalog_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["research-catalog-reads"])

    @router.get("/api/v1/universes/{universe_key}")
    def universe(universe_key: str) -> dict[str, Any]:
        return read_model.universe_members(database, universe_key)

    @router.get("/api/v1/features/latest")
    def features(universe_key: str = "core", limit: int = 200) -> dict[str, Any]:
        return read_model.latest_features(database, universe_key, limit)

    @router.get("/api/v1/factors")
    def factors() -> dict[str, Any]:
        return read_model.factor_registry(database)

    @router.get("/api/v1/factors/evaluations")
    def factor_evaluation_history(universe_key: str = "core", limit: int = 100) -> dict[str, Any]:
        return read_model.factor_evaluations(database, universe_key, limit)

    @router.get("/api/v1/strategies")
    def strategies() -> dict[str, Any]:
        return read_model.strategy_registry(database)

    @router.get("/api/v1/strategies/experiments")
    def experiments(universe_key: str = "core", limit: int = 50) -> dict[str, Any]:
        return read_model.strategy_experiments(database, universe_key, limit)

    @router.get("/api/v1/data-quality/issues")
    def quality_issues(limit: int = 100) -> dict[str, Any]:
        return read_model.data_quality_issues(database, limit)

    return router
