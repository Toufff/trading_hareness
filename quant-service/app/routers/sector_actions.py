"""Bounded sector synchronization and research write routes.

Only HTTP contract assembly lives here.  The injected services retain their
existing provider capability gates, local executor backpressure handling,
raw-first persistence and exact-membership constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..request_models import (
    BoardResearchRunRequest,
    AllBoardMemberBackfillRequest,
    ConceptCandidateSyncRequest,
    ConceptMemberBackfillRequest,
    ConceptMemberSyncRequest,
    EastmoneyBoardMemberSyncRequest,
    IntradaySectorReportRequest,
    SectorCatalogSyncRequest,
    SectorFlowSyncRequest,
)


@dataclass(frozen=True)
class SectorActionDependencies:
    sync_catalog: Callable[[SectorCatalogSyncRequest], Awaitable[dict[str, Any]]]
    sync_eastmoney_members: Callable[[EastmoneyBoardMemberSyncRequest], Awaitable[dict[str, Any]]]
    intraday_report: Callable[[IntradaySectorReportRequest], Awaitable[dict[str, Any]]]
    sync_industry_flows: Callable[[SectorFlowSyncRequest], Awaitable[dict[str, Any]]]
    sync_concepts: Callable[[SectorFlowSyncRequest], Awaitable[dict[str, Any]]]
    sync_concept_members: Callable[[ConceptMemberSyncRequest], Awaitable[dict[str, Any]]]
    backfill_concept_members: Callable[[ConceptMemberBackfillRequest], Awaitable[dict[str, Any]]]
    sync_concept_candidates: Callable[[ConceptCandidateSyncRequest], Awaitable[dict[str, Any]]]
    run_board_research: Callable[[BoardResearchRunRequest], Awaitable[dict[str, Any]]]
    backfill_all_members: Callable[[AllBoardMemberBackfillRequest], Awaitable[dict[str, Any]]] | None = None


def build_sector_actions_router(deps: SectorActionDependencies) -> APIRouter:
    """Build URL-compatible sector actions without provider/application globals."""
    router = APIRouter(tags=["sector-actions"])

    @router.post("/api/v1/market/sectors/sync")
    async def sync_catalog(payload: SectorCatalogSyncRequest) -> dict[str, Any]:
        return await deps.sync_catalog(payload)

    @router.post("/api/v1/market/sectors/eastmoney/members/sync")
    async def sync_eastmoney_members(payload: EastmoneyBoardMemberSyncRequest) -> dict[str, Any]:
        return await deps.sync_eastmoney_members(payload)

    @router.post("/api/v1/market/sectors/intraday/report")
    async def intraday_report(payload: IntradaySectorReportRequest) -> dict[str, Any]:
        return await deps.intraday_report(payload)

    @router.post("/api/v1/market/sectors/flows/sync")
    async def sync_industry_flows(payload: SectorFlowSyncRequest) -> dict[str, Any]:
        return await deps.sync_industry_flows(payload)

    @router.post("/api/v1/market/sectors/concepts/sync")
    async def sync_concepts(payload: SectorFlowSyncRequest) -> dict[str, Any]:
        return await deps.sync_concepts(payload)

    @router.post("/api/v1/market/sectors/concepts/members/sync")
    async def sync_concept_members(payload: ConceptMemberSyncRequest) -> dict[str, Any]:
        return await deps.sync_concept_members(payload)

    @router.post("/api/v1/market/sectors/concepts/members/backfill/run")
    async def backfill_concept_members(payload: ConceptMemberBackfillRequest) -> dict[str, Any]:
        return await deps.backfill_concept_members(payload)

    @router.post("/api/v1/market/sectors/members/backfill/run")
    async def backfill_all_members(payload: AllBoardMemberBackfillRequest) -> dict[str, Any]:
        if deps.backfill_all_members is None:
            return {"status": "blocked", "reason": "all-board member backfill is not configured"}
        return await deps.backfill_all_members(payload)

    @router.post("/api/v1/market/sectors/concepts/candidates/sync")
    async def sync_concept_candidates(payload: ConceptCandidateSyncRequest) -> dict[str, Any]:
        return await deps.sync_concept_candidates(payload)

    @router.post("/api/v1/market/sectors/concepts/research/run")
    async def board_research(payload: BoardResearchRunRequest) -> dict[str, Any]:
        return await deps.run_board_research(payload)

    return router


__all__ = ["SectorActionDependencies", "build_sector_actions_router"]
