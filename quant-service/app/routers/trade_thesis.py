"""Research-only trade-thesis reads and governed immutable writes."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..trade_thesis.repository import ThesisConflict, ThesisNotFound


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_run_id: str = Field(min_length=1, max_length=200)
    cutoff_at: datetime | None = None
    symbols: list[str] | None = None
    namespace: Literal["capture", "shadow", "advisory"] = "shadow"


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    changes: dict[str, Any]
    actor: str = Field(min_length=1, max_length=200)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["approve", "reject", "needs_evidence"]
    reviewer: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)
    evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thesis_revision: int = Field(ge=1)
    account_key: str = Field(min_length=1, max_length=64)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    position_episode_id: str = Field(min_length=1, max_length=200)
    plan_id: UUID
    binding_source: str = Field(default="manual_api", min_length=1, max_length=120)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


@dataclass(frozen=True)
class TradeThesisDependencies:
    database: Any
    async_database: Any
    run_database: Callable[..., Awaitable[Any]]
    list_theses: Callable[..., Awaitable[list[dict[str, Any]]]]
    timeline: Callable[..., Awaitable[dict[str, Any] | None]]
    propose_change: Callable[..., dict[str, Any]]
    review_change: Callable[..., dict[str, Any]]
    evaluate_source_run: Callable[..., dict[str, Any]]
    create_binding: Callable[..., dict[str, Any]] | None = None
    load_bound_plan: Callable[..., dict[str, Any]] | None = None


def runtime_trade_thesis_dependencies(database: Any, async_database: Any,
                                      run_database: Callable[..., Awaitable[Any]]) -> TradeThesisDependencies:
    from ..trade_thesis.read_repository import list_theses, thesis_timeline
    from ..trade_thesis.repository import propose_change, review_change
    from ..trade_thesis.bindings import create_binding, load_bound_plan

    def evaluate_source_run(db: Any, source_run_id: str, cutoff_at: datetime | None = None,
                            symbols: list[str] | None = None, namespace: str = "shadow") -> dict[str, Any]:
        from ..trade_thesis.scan_adapter import evaluate_source_run as evaluate
        return evaluate(db, source_run_id, cutoff_at=cutoff_at, symbols=symbols, namespace=namespace)

    return TradeThesisDependencies(
        database=database, async_database=async_database, run_database=run_database,
        list_theses=list_theses, timeline=thesis_timeline, propose_change=propose_change,
        review_change=review_change, evaluate_source_run=evaluate_source_run,
        create_binding=create_binding, load_bound_plan=load_bound_plan,
    )


def build_trade_thesis_router(deps: TradeThesisDependencies) -> APIRouter:
    router = APIRouter(prefix="/api/v1/research/theses", tags=["trade-thesis-research"])

    @router.get("")
    async def read_theses(symbol: str | None = None, as_of: datetime | None = None,
                          namespace: Literal["capture", "shadow", "advisory"] = "shadow",
                          limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
        rows = await deps.list_theses(
            deps.async_database, symbol=symbol, as_of=as_of, namespace=namespace, limit=limit,
        )
        return {"items": rows, "count": len(rows), "as_of": as_of,
                "namespace": namespace, "live_effect": "none"}

    @router.get("/{thesis_id}/timeline")
    async def read_timeline(thesis_id: str, as_of: datetime | None = None,
                            namespace: Literal["capture", "shadow", "advisory"] = "shadow",
                            limit: int = Query(default=500, ge=1, le=1000)) -> dict[str, Any]:
        result = await deps.timeline(
            deps.async_database, thesis_id, as_of=as_of, namespace=namespace, limit=limit,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="trade thesis not found")
        return {**result, "as_of": as_of, "namespace": namespace, "live_effect": "none"}

    @router.post("/evaluate")
    async def evaluate(payload: EvaluateRequest) -> dict[str, Any]:
        # This callback resolves the stored run and its PIT evidence.  The HTTP
        # contract deliberately has no raw evidence field clients could label verified.
        try:
            call = functools.partial(
                deps.evaluate_source_run, deps.database, payload.source_run_id,
                cutoff_at=payload.cutoff_at, symbols=payload.symbols, namespace=payload.namespace,
            )
            result = await deps.run_database(
                call, timeout_seconds=30,
            )
        except ThesisNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {**result, "decision_binding": False, "live_effect": "none"}

    async def _write(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return await deps.run_database(call, timeout_seconds=10)
        except ThesisNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ThesisConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post("/{thesis_id}/changes")
    async def propose(thesis_id: str, payload: ChangeRequest) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with deps.database.transaction() as connection:
                return deps.propose_change(
                    connection, thesis_id, expected_revision=payload.expected_revision,
                    changes=payload.changes, actor=payload.actor,
                )
        result = await _write(run)
        return {**result, "active_changed": False, "decision_binding": False, "live_effect": "none"}

    @router.post("/{thesis_id}/reviews")
    async def review(thesis_id: str, payload: ReviewRequest) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with deps.database.transaction() as connection:
                return deps.review_change(
                    connection, thesis_id, revision=payload.revision,
                    proposal_hash=payload.proposal_hash, verdict=payload.verdict,
                    reviewer=payload.reviewer, reason=payload.reason,
                    evidence_hash=payload.evidence_hash,
                )
        result = await _write(run)
        return {**result, "active_changed": payload.verdict == "approve",
                "decision_binding": False, "live_effect": "none"}

    @router.get("/{thesis_id}/binding")
    async def read_binding(thesis_id: str, account_key: str | None = None,
                           symbol: str | None = None, as_of: datetime | None = None) -> dict[str, Any]:
        if deps.load_bound_plan is None:
            raise HTTPException(status_code=503, detail="trade thesis binding service unavailable")

        def run() -> dict[str, Any]:
            with deps.database.transaction() as connection:
                return deps.load_bound_plan(
                    connection, thesis_id, account_key=account_key, symbol=symbol, as_of=as_of,
                )
        result = await deps.run_database(run, timeout_seconds=10)
        return {**result, "as_of": as_of, "live_effect": "none"}

    @router.post("/{thesis_id}/bindings")
    async def bind(thesis_id: str, payload: BindingRequest) -> dict[str, Any]:
        if deps.create_binding is None:
            raise HTTPException(status_code=503, detail="trade thesis binding service unavailable")

        def run() -> dict[str, Any]:
            with deps.database.transaction() as connection:
                return deps.create_binding(
                    connection, thesis_id, thesis_revision=payload.thesis_revision,
                    account_key=payload.account_key, symbol=payload.symbol,
                    position_episode_id=payload.position_episode_id, plan_id=payload.plan_id,
                    binding_source=payload.binding_source, evidence_refs=payload.evidence_refs,
                )
        result = await _write(run)
        return {**result, "decision_binding": False, "live_effect": "none"}

    return router


__all__ = ["BindingRequest", "ChangeRequest", "EvaluateRequest", "ReviewRequest", "TradeThesisDependencies",
           "build_trade_thesis_router", "runtime_trade_thesis_dependencies"]
