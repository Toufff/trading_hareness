"""Read-only research-readiness and framework routes."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from ..request_models import HistoricalCoverageEstimateRequest


def training_roadmap_payload() -> dict[str, Any]:
    """Describe promotion prerequisites without claiming data is available."""
    return {
        "status": "planned",
        "stages": [
            {"stage": "data-readiness", "gate": "3+ years point-in-time, delisting-aware daily data plus 12+ months forward labels", "compute": "CPU"},
            {"stage": "factor-selection", "gate": "walk-forward IC, turnover and cost gates", "compute": "CPU"},
            {"stage": "supervised-model", "gate": "stable out-of-sample results against multi-factor baseline", "compute": "1x H100 80GB is sufficient"},
            {"stage": "ensemble-and-sweep", "gate": "purged walk-forward and regime stress tests", "compute": "4x H100 80GB"},
            {"stage": "RL-research", "gate": "A-share constrained environment and no superiority claim without live shadow results", "compute": "4-8x H100 80GB"},
        ],
        "policy": "GPU models remain offline research artifacts until promotion gates pass; the online service never places orders.",
    }


def build_research_readiness_router(
    database: Any,
    historical_estimate_fn: Callable[[HistoricalCoverageEstimateRequest], dict[str, Any]],
    feature_readiness_fn: Callable[[Any], dict[str, Any]],
    replay_readiness_fn: Callable[[Any], dict[str, Any]],
) -> APIRouter:
    """Build read-only routes with explicit repository/service dependencies."""
    router = APIRouter(tags=["research-readiness"])

    @router.get("/api/v1/research-frameworks")
    def research_frameworks() -> dict[str, Any]:
        with database.transaction() as connection:
            rows = connection.execute(
                "SELECT framework_key,label,role,integration_mode,status,license_note,prerequisites,metadata,updated_at FROM quant.research_frameworks ORDER BY framework_key"
            ).fetchall()
        return {"items": rows}

    @router.get("/api/v1/training/roadmap")
    def training_roadmap() -> dict[str, Any]:
        return training_roadmap_payload()

    @router.get("/api/v1/data-readiness/history-estimate")
    def historical_data_estimate(
        years: int = 3, include_minute: bool = False, universe_symbols: int | None = None,
    ) -> dict[str, Any]:
        return historical_estimate_fn(HistoricalCoverageEstimateRequest(
            years=years, include_minute=include_minute, universe_symbols=universe_symbols,
        ))

    @router.get("/api/v1/data-readiness/features")
    def feature_readiness() -> dict[str, Any]:
        with database.transaction() as connection:
            return feature_readiness_fn(connection)

    @router.get("/api/v1/data-readiness/replay")
    def replay_readiness() -> dict[str, Any]:
        return replay_readiness_fn(database)

    return router
