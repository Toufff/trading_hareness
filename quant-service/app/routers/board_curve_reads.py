"""Read-only API assembly for stored board-flow curves."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter

from ..board_curve_read_model import intraday_board_flow_curves, latest_close_sector_review_report


def build_board_curve_reads_router(database: Any, curve_retention_days: Any, rotation_retention_days: Any) -> APIRouter:
    router = APIRouter(tags=["board-curve-reads"])

    @router.get("/api/v1/market/sectors/intraday/curves")
    def curves(
        trade_date: date | None = None,
        taxonomy: Literal["industry", "concept"] = "industry",
        since: datetime | None = None,
    ) -> dict[str, Any]:
        return intraday_board_flow_curves(
            database, trade_date, taxonomy, since,
            curve_retention_days=curve_retention_days(),
            rotation_retention_days=rotation_retention_days(),
        )

    @router.get("/api/v1/market/sectors/review/report/latest")
    def latest_review() -> dict[str, Any]:
        return latest_close_sector_review_report(database)

    return router
