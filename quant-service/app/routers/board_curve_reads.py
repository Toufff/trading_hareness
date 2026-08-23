"""Read-only API assembly for stored board-flow curves."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter

from ..async_board_curve_read_repository import intraday_board_flow_curves as async_intraday_board_flow_curves
from ..async_board_curve_read_repository import latest_close_sector_review_report as async_latest_close_sector_review_report
from ..board_curve_read_model import intraday_board_flow_curves, latest_close_sector_review_report


def build_board_curve_reads_router(
    database: Any,
    curve_retention_days: Any,
    rotation_retention_days: Any,
    *,
    async_database: Any | None = None,
    async_curves_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    async_latest_review_fn: Callable[[Any], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["board-curve-reads"])

    @router.get("/api/v1/market/sectors/intraday/curves")
    async def curves(
        trade_date: date | None = None,
        taxonomy: Literal["industry", "concept"] = "industry",
        since: datetime | None = None,
    ) -> dict[str, Any]:
        if async_database is not None:
            return await (async_curves_fn or async_intraday_board_flow_curves)(
                async_database, trade_date, taxonomy, since,
                curve_retention_days=curve_retention_days(), rotation_retention_days=rotation_retention_days(),
            )
        return intraday_board_flow_curves(
            database, trade_date, taxonomy, since,
            curve_retention_days=curve_retention_days(),
            rotation_retention_days=rotation_retention_days(),
        )

    @router.get("/api/v1/market/sectors/review/report/latest")
    async def latest_review() -> dict[str, Any]:
        if async_database is not None:
            return await (async_latest_review_fn or async_latest_close_sector_review_report)(async_database)
        return latest_close_sector_review_report(database)

    return router
