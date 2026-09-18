"""Read-only HTTP boundary for machine-derived trade discipline.

Three GETs and nothing else: the plans an account currently holds, one plan by
id, and the newest evaluation of one plan.  Plans are written by
``scripts/trade-discipline.py``; there is deliberately no write route, because a
discipline plan is an append-only derivation from evidence rather than something
a client posts.

Every projection is awaited from the native async read repository so a dashboard
refresh never opens a blocking transaction on the event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query

PLAN_STATUSES = ("active", "rejected_by_quality", "superseded", "expired")
DEFAULT_STATUSES = ("active", "rejected_by_quality")
BOUNDARY = "research_only_human_decision_support"


@dataclass(frozen=True)
class TradeDisciplineDependencies:
    async_database: Any
    latest_plans: Callable[..., Awaitable[list[dict[str, Any]]]]
    read_plan: Callable[..., Awaitable[dict[str, Any] | None]]
    latest_evaluation: Callable[..., Awaitable[dict[str, Any] | None]]


def parse_statuses(raw: str | None) -> tuple[str, ...]:
    """``active,expired`` -> a validated tuple; an unknown status is a 422."""
    if not raw or not raw.strip():
        return DEFAULT_STATUSES
    requested = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = [status for status in requested if status not in PLAN_STATUSES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown plan status: {','.join(unknown)}")
    return requested or DEFAULT_STATUSES


def build_trade_discipline_router(deps: TradeDisciplineDependencies) -> APIRouter:
    router = APIRouter(tags=["trade-discipline"])

    @router.get("/api/v1/discipline/plans/latest")
    async def read_latest_discipline_plans(
        account_key: str,
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        statuses = parse_statuses(status)
        items = await deps.latest_plans(deps.async_database, account_key, statuses=statuses, limit=limit)
        return {"account_key": account_key, "statuses": list(statuses), "limit": limit,
                "count": len(items), "items": items, "live_orders": False, "boundary": BOUNDARY}

    @router.get("/api/v1/discipline/plans/{plan_id}")
    async def read_discipline_plan(plan_id: str) -> dict[str, Any]:
        plan = await deps.read_plan(deps.async_database, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="no discipline plan with that id")
        return {"plan": plan, "live_orders": False, "boundary": BOUNDARY}

    @router.get("/api/v1/discipline/evaluations/latest")
    async def read_latest_discipline_evaluation(
        plan_id: str,
        basis: str | None = None,
    ) -> dict[str, Any]:
        if basis is not None and basis not in {"daily", "minute"}:
            raise HTTPException(status_code=422, detail="basis must be daily or minute")
        evaluation = await deps.latest_evaluation(deps.async_database, plan_id, basis=basis)
        if evaluation is None:
            raise HTTPException(status_code=404, detail="no evaluation for that plan")
        return {"evaluation": evaluation, "live_orders": False, "boundary": BOUNDARY}

    return router


__all__ = ["BOUNDARY", "DEFAULT_STATUSES", "PLAN_STATUSES", "TradeDisciplineDependencies",
           "build_trade_discipline_router", "parse_statuses"]
