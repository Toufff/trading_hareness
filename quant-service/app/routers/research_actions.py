"""Local research-governance write routes.

Each dependency is already an async, bounded service wrapper supplied by the
composition root.  This router therefore cannot create a database connection,
fetch a provider, or silently start historical ingestion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter, Request

from ..request_models import (
    AnalystResearchProfileRequest,
    AnalystSyncCursorUpdate,
    AnalystSyncGlobalCursorUpdate,
    ClaimReviewRequest,
    FactorEvaluationRequest,
    FetchRunReconcileRequest,
    GenerateRequest,
    RemoteReportImport,
    RemoteReportReprocessRequest,
    RemoteAnalystMessageImport,
    RemoteArchiveSyncRequest,
    RemoteMessageReprocessRequest,
    SnapshotRequest,
    StrategyBacktestRequest,
    UniverseUpdateRequest,
)


@dataclass(frozen=True)
class ResearchActionDependencies:
    analyse_ingestion: Callable[[UUID], Awaitable[dict[str, Any]]]
    import_remote_report: Callable[[RemoteReportImport], Awaitable[dict[str, Any]]]
    import_remote_message: Callable[[RemoteAnalystMessageImport], Awaitable[dict[str, Any]]]
    reprocess_remote_reports: Callable[[RemoteReportReprocessRequest], Awaitable[dict[str, Any]]]
    reprocess_remote_messages: Callable[[RemoteMessageReprocessRequest], Awaitable[dict[str, Any]]]
    review_claim: Callable[[UUID, ClaimReviewRequest], Awaitable[dict[str, Any]]]
    update_universe: Callable[[UniverseUpdateRequest], Awaitable[dict[str, Any]]]
    build_features: Callable[[GenerateRequest], Awaitable[dict[str, Any]]]
    evaluate_factors: Callable[[FactorEvaluationRequest], Awaitable[dict[str, Any]]]
    backtest: Callable[[StrategyBacktestRequest], Awaitable[dict[str, Any]]]
    reconcile_fetch_runs: Callable[[FetchRunReconcileRequest], Awaitable[dict[str, Any]]]
    build_snapshot: Callable[[SnapshotRequest], Awaitable[dict[str, Any]]]
    update_analyst_research_profile: Callable[[str, AnalystResearchProfileRequest], Awaitable[dict[str, Any]]]
    update_analyst_sync_cursor: Callable[[AnalystSyncCursorUpdate], Awaitable[dict[str, Any]]]
    update_analyst_global_sync_cursor: Callable[[AnalystSyncGlobalCursorUpdate], Awaitable[dict[str, Any]]]
    sync_remote_archive: Callable[[RemoteArchiveSyncRequest, str | None], Awaitable[dict[str, Any]]]


def build_research_actions_router(deps: ResearchActionDependencies) -> APIRouter:
    """Build local-only research writes while preserving public URL contracts."""
    router = APIRouter(tags=["research-actions"])

    @router.post("/api/v1/analysis/jobs/{analysis_id}/run")
    async def analyse_ingestion(analysis_id: UUID) -> dict[str, Any]:
        return await deps.analyse_ingestion(analysis_id)

    @router.post("/api/v1/remote-archive/reports/import")
    async def import_remote_report(payload: RemoteReportImport) -> dict[str, Any]:
        return await deps.import_remote_report(payload)

    @router.post("/api/v1/remote-archive/messages/import")
    async def import_remote_message(payload: RemoteAnalystMessageImport) -> dict[str, Any]:
        return await deps.import_remote_message(payload)

    @router.post("/api/v1/remote-archive/reports/reprocess")
    async def reprocess_remote_reports(payload: RemoteReportReprocessRequest) -> dict[str, Any]:
        return await deps.reprocess_remote_reports(payload)

    @router.post("/api/v1/remote-archive/messages/reprocess")
    async def reprocess_remote_messages(payload: RemoteMessageReprocessRequest) -> dict[str, Any]:
        return await deps.reprocess_remote_messages(payload)

    @router.post("/api/v1/remote-archive/sync")
    async def sync_remote_archive(payload: RemoteArchiveSyncRequest, request: Request) -> dict[str, Any]:
        # n8n uses its existing encrypted archive bearer credential only for
        # this local trigger. The service forwards it in-memory to the remote
        # text API and never persists or echoes it.
        return await deps.sync_remote_archive(payload, request.headers.get("Authorization"))

    @router.post("/api/v1/claim-review/{review_id}")
    async def review_claim(review_id: UUID, payload: ClaimReviewRequest) -> dict[str, Any]:
        return await deps.review_claim(review_id, payload)

    @router.post("/api/v1/universes/members")
    async def update_universe(payload: UniverseUpdateRequest) -> dict[str, Any]:
        return await deps.update_universe(payload)

    @router.post("/api/v1/features/build")
    async def build_features(payload: GenerateRequest) -> dict[str, Any]:
        return await deps.build_features(payload)

    @router.post("/api/v1/factors/evaluate")
    async def evaluate_factors(payload: FactorEvaluationRequest) -> dict[str, Any]:
        return await deps.evaluate_factors(payload)

    @router.post("/api/v1/strategies/backtest")
    async def backtest(payload: StrategyBacktestRequest) -> dict[str, Any]:
        return await deps.backtest(payload)

    @router.post("/api/v1/operations/fetch-runs/reconcile-stale")
    async def reconcile_fetch_runs(payload: FetchRunReconcileRequest) -> dict[str, Any]:
        return await deps.reconcile_fetch_runs(payload)

    @router.post("/api/v1/data-snapshots/build")
    async def build_snapshot(payload: SnapshotRequest) -> dict[str, Any]:
        return await deps.build_snapshot(payload)

    @router.put("/api/v1/analyst-research/profiles/{analyst_id}")
    async def update_analyst_research_profile(analyst_id: str, payload: AnalystResearchProfileRequest) -> dict[str, Any]:
        return await deps.update_analyst_research_profile(analyst_id, payload)

    @router.put("/api/v1/remote-archive/sync-cursors")
    async def update_analyst_sync_cursor(payload: AnalystSyncCursorUpdate) -> dict[str, Any]:
        return await deps.update_analyst_sync_cursor(payload)

    @router.put("/api/v1/remote-archive/sync-cursors-global")
    async def update_analyst_global_sync_cursor(payload: AnalystSyncGlobalCursorUpdate) -> dict[str, Any]:
        return await deps.update_analyst_global_sync_cursor(payload)

    return router


__all__ = ["ResearchActionDependencies", "build_research_actions_router"]
