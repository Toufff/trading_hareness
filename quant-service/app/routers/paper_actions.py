"""Explicit write routes for manual-only paper research operations."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter

from ..request_models import PaperAccountConfigureRequest, PaperDecisionAcceptRequest


def build_paper_actions_router(database: Any, configure_fn: Callable[..., dict[str, Any]],
                               accept_fn: Callable[..., dict[str, Any]]) -> APIRouter:
    router = APIRouter(tags=["paper-research-actions"])

    @router.put("/api/v1/paper/accounts")
    def configure_account(payload: PaperAccountConfigureRequest) -> dict[str, Any]:
        with database.transaction() as connection:
            account = configure_fn(connection, account_key=payload.account_key,
                                   initial_cash=Decimal(str(payload.initial_cash)),
                                   configured_by=payload.configured_by,
                                   metadata={"mode": "manual_paper_only"})
        return {"status": "configured", "account": account, "live_orders": False}

    @router.post("/api/v1/paper/decisions/{decision_id}/accept")
    def accept_decision(decision_id: UUID, payload: PaperDecisionAcceptRequest) -> dict[str, Any]:
        with database.transaction() as connection:
            result = accept_fn(connection, decision_id=decision_id, quantity=payload.quantity,
                               accepted_at=datetime.now(timezone.utc), account_key=payload.account_key)
        return {**result, "live_orders": False, "boundary": "manual paper simulation only; no broker client"}

    return router


__all__ = ["build_paper_actions_router"]
