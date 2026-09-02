"""Actual-portfolio ingestion and personal decision read routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from ..personal_decision_contracts import BrokerPortfolioSnapshotInput, PersonalTradePlanInput
from ..personal_decision_repository import ImmutableDecisionFactConflict
from ..runtime_executors import run_database_blocking


@dataclass(frozen=True)
class PersonalDecisionDependencies:
    database: Any
    async_database: Any
    persist_snapshot: Callable[[Any, BrokerPortfolioSnapshotInput], dict[str, Any]]
    persist_plan: Callable[[Any, PersonalTradePlanInput], dict[str, Any]]
    latest_snapshot: Callable[[Any, str], Awaitable[dict[str, Any] | None]]
    latest_brief: Callable[[Any, str], Awaitable[dict[str, Any]]]
    latest_research: Callable[[Any], Awaitable[dict[str, Any]]]


def build_personal_decisions_router(deps: PersonalDecisionDependencies) -> APIRouter:
    router = APIRouter(tags=["personal-decision-support"])

    @router.post("/api/v1/personal/portfolio-snapshots")
    async def record_portfolio_snapshot(payload: BrokerPortfolioSnapshotInput) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with deps.database.transaction() as connection:
                return deps.persist_snapshot(connection, payload)
        try:
            result = await run_database_blocking(run, timeout_seconds=3)
        except ImmutableDecisionFactConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {**result, "live_orders": False, "boundary": "read_only_broker_fact"}

    @router.get("/api/v1/personal/portfolio-snapshots/latest")
    async def read_latest_portfolio_snapshot(account_key: str) -> dict[str, Any]:
        snapshot = await deps.latest_snapshot(deps.async_database, account_key)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="no verified broker snapshot for account")
        return snapshot

    @router.post("/api/v1/personal/trade-plans")
    async def record_trade_plan(payload: PersonalTradePlanInput) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with deps.database.transaction() as connection:
                return deps.persist_plan(connection, payload)
        try:
            result = await run_database_blocking(run, timeout_seconds=3)
        except ImmutableDecisionFactConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {**result, "live_orders": False, "boundary": "human_decision_support_only"}

    @router.get("/api/v1/personal/decision-briefs/latest")
    async def read_latest_personal_decision_brief(account_key: str) -> dict[str, Any]:
        return await deps.latest_brief(deps.async_database, account_key)

    @router.get("/api/v1/personal/decision-research/latest")
    async def read_latest_decision_research() -> dict[str, Any]:
        return await deps.latest_research(deps.async_database)

    return router


__all__ = ["PersonalDecisionDependencies", "build_personal_decisions_router"]
