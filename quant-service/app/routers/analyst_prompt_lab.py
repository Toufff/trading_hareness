"""Read/write routes for the human-gated analyst Prompt Lab."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter

from ..async_analyst_prompt_lab_read_repository import status as async_status
from ..request_models import AnalystPromptEvaluateRequest, AnalystPromptGoldLabelRequest


def _status_sync(database: Any, limit: int) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 500))
    with database.transaction() as connection:
        candidates = connection.execute(
            """SELECT c.candidate_id,c.analyst_id,c.variant_key,c.variant_version,c.status,c.created_at,
                      o.scope,o.subject_key,o.action,o.direction,o.strategy_available_at,
                      l.label,l.direction_correct,l.action_executable,l.reviewer,l.labelled_at
                 FROM quant.analyst_prompt_candidates c
                 JOIN quant.analyst_observations o USING(observation_id)
                 LEFT JOIN quant.analyst_prompt_gold_labels l USING(candidate_id)
                ORDER BY c.created_at DESC LIMIT %s""", (bounded,)
        ).fetchall()
        evaluations = connection.execute(
            """SELECT evaluation_id,variant_key,variant_version,cutoff_at,status,sample_count,metrics,created_at
                 FROM quant.analyst_prompt_evaluation_runs ORDER BY cutoff_at DESC LIMIT %s""", (bounded,)
        ).fetchall()
        outcomes = connection.execute(
            """SELECT methodology_version,horizon_minutes,status,count(*)::int count,
                      avg(directional_return) avg_directional_return
                 FROM quant.analyst_intraday_outcomes
                GROUP BY methodology_version,horizon_minutes,status
                ORDER BY methodology_version,horizon_minutes,status"""
        ).fetchall()
    return {"candidates": [dict(row) for row in candidates], "evaluations": [dict(row) for row in evaluations],
            "intraday_outcomes": [dict(row) for row in outcomes], "live_effect": "none",
            "boundary": "human labels and out-of-sample promotion required"}


def build_analyst_prompt_lab_router(database: Any, materialize_fn: Callable[..., dict[str, Any]],
                                    label_fn: Callable[..., dict[str, Any]],
                                    evaluate_fn: Callable[..., dict[str, Any]],
                                    outcome_fn: Callable[..., dict[str, Any]], *,
                                    async_database: Any | None = None,
                                    async_status_fn: Callable[[Any, int], Awaitable[dict[str, Any]]] | None = None) -> APIRouter:
    router = APIRouter(tags=["analyst-prompt-lab"])

    @router.get("/api/v1/analyst-prompt-lab/status")
    async def status(limit: int = 100) -> dict[str, Any]:
        if async_database is not None:
            return await (async_status_fn or async_status)(async_database, limit)
        return _status_sync(database, limit)

    @router.post("/api/v1/analyst-prompt-lab/materialize")
    def materialize() -> dict[str, Any]:
        with database.transaction() as connection:
            return materialize_fn(connection, cutoff_at=datetime.now(timezone.utc))

    @router.post("/api/v1/analyst-prompt-lab/candidates/{candidate_id}/label")
    def label(candidate_id: UUID, payload: AnalystPromptGoldLabelRequest) -> dict[str, Any]:
        with database.transaction() as connection:
            item = label_fn(connection, candidate_id=candidate_id, label=payload.label,
                            direction_correct=payload.direction_correct, action_executable=payload.action_executable,
                            reviewer=payload.reviewer, notes=payload.notes)
        return {"status": "labelled", "item": item, "live_effect": "none"}

    @router.post("/api/v1/analyst-prompt-lab/evaluate/{variant_key}")
    def evaluate(variant_key: str, payload: AnalystPromptEvaluateRequest) -> dict[str, Any]:
        with database.transaction() as connection:
            return evaluate_fn(connection, variant_key=variant_key,
                               cutoff_at=payload.cutoff_at or datetime.now(timezone.utc),
                               minimum_labels=payload.minimum_labels)

    @router.post("/api/v1/analyst-intraday-outcomes/recompute")
    def recompute_intraday_outcomes() -> dict[str, Any]:
        with database.transaction() as connection:
            return outcome_fn(connection, cutoff_at=datetime.now(timezone.utc))

    return router


__all__ = ["build_analyst_prompt_lab_router"]
