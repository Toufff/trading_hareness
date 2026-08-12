"""Read-only route for settled intraday signal outcomes."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from ..intraday_outcome_read_model import latest_intraday_outcomes


def build_intraday_outcome_reads_router(
    database: Any,
    market_context_batch_fn: Callable[..., dict[str, Any]],
    attribution_fn: Callable[..., dict[str, Any]],
    attribution_summary_fn: Callable[..., dict[str, Any]],
) -> APIRouter:
    router = APIRouter(tags=["intraday-outcome-reads"])

    @router.get("/api/v1/intraday/outcomes/latest")
    def latest(limit: int = 100) -> dict[str, Any]:
        return latest_intraday_outcomes(
            database, limit,
            market_context_batch_fn=market_context_batch_fn,
            attribution_fn=attribution_fn,
            attribution_summary_fn=attribution_summary_fn,
        )

    return router
