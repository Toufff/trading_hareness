"""Explicit write routes for manual-only paper research operations."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter

from ..request_models import PaperAccountConfigureRequest, PaperDecisionAcceptRequest
from ..runtime_executors import run_database_blocking


def build_paper_actions_router(database: Any, configure_fn: Callable[..., dict[str, Any]],
                               accept_fn: Callable[..., dict[str, Any]]) -> APIRouter:
    router = APIRouter(tags=["paper-research-actions"])

    @router.put("/api/v1/paper/accounts")
    async def configure_account(payload: PaperAccountConfigureRequest) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with database.transaction() as connection:
                return configure_fn(connection, account_key=payload.account_key,
                                    initial_cash=Decimal(str(payload.initial_cash)),
                                    configured_by=payload.configured_by,
                                    metadata={"mode": "manual_paper_only"})
        account = await run_database_blocking(run, timeout_seconds=3)
        return {"status": "configured", "account": account, "live_orders": False}

    @router.post("/api/v1/paper/decisions/{decision_id}/accept")
    async def accept_decision(decision_id: UUID, payload: PaperDecisionAcceptRequest) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with database.transaction() as connection:
                return accept_fn(connection, decision_id=decision_id, quantity=payload.quantity,
                                 accepted_at=datetime.now(timezone.utc), account_key=payload.account_key)
        result = await run_database_blocking(run, timeout_seconds=3)
        return {**result, "live_orders": False, "boundary": "manual paper simulation only; no broker client"}

    return router


__all__ = ["build_paper_actions_router"]
