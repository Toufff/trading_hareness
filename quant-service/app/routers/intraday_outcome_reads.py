"""Read-only route for settled intraday signal outcomes."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from ..async_intraday_outcome_read_repository import latest_intraday_outcomes as async_latest_intraday_outcomes
from ..intraday_outcome_read_model import latest_intraday_outcomes


def build_intraday_outcome_reads_router(
    database: Any,
    market_context_batch_fn: Callable[..., dict[str, Any]],
    attribution_fn: Callable[..., dict[str, Any]],
    attribution_summary_fn: Callable[..., dict[str, Any]],
    *,
    async_database: Any | None = None,
    market_context_from_board_report_fn: Callable[..., dict[str, Any]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["intraday-outcome-reads"])

    @router.get("/api/v1/intraday/outcomes/latest")
    async def latest(limit: int = 100) -> dict[str, Any]:
        if async_database is not None and market_context_from_board_report_fn is not None:
            return await async_latest_intraday_outcomes(
                async_database, limit,
                market_context_from_board_report_fn=market_context_from_board_report_fn,
                attribution_fn=attribution_fn,
                attribution_summary_fn=attribution_summary_fn,
            )
        return latest_intraday_outcomes(
            database, limit,
            market_context_batch_fn=market_context_batch_fn,
            attribution_fn=attribution_fn,
            attribution_summary_fn=attribution_summary_fn,
        )

    return router
