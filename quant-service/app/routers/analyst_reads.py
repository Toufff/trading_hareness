"""Read-only routes for already extracted analyst text evidence."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from ..analyst_read_model import analyst_claims, claim_review_queue, remote_messages, remote_reports
from ..remote_archive import analyst_global_sync_cursor, analyst_sync_cursor


def build_analyst_reads_router(
    database: Any,
    remote_state_fn: Callable[[Any], dict[str, Any]],
    factor_summary_fn: Callable[[Any, date, int], dict[str, Any]],
) -> APIRouter:
    router = APIRouter(tags=["analyst-reads"])

    @router.get("/api/v1/remote-archive/state")
    def remote_archive_state() -> dict[str, Any]:
        return remote_state_fn(database)

    @router.get("/api/v1/remote-archive/reports")
    def remote_archive_reports(limit: int = 30, offset: int = 0) -> dict[str, Any]:
        return remote_reports(database, limit, offset)

    @router.get("/api/v1/remote-archive/messages")
    def remote_archive_messages(analyst_id: str | None = None, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        return remote_messages(database, analyst_id, limit, offset)

    @router.get("/api/v1/remote-archive/sync-cursors/{stream_key}/{analyst_id}")
    def sync_cursor(stream_key: str, analyst_id: str) -> dict[str, Any]:
        return analyst_sync_cursor(database, stream_key, analyst_id)

    @router.get("/api/v1/remote-archive/sync-cursors-global/{stream_key}")
    def global_sync_cursor(stream_key: str) -> dict[str, Any]:
        return analyst_global_sync_cursor(database, stream_key)

    @router.get("/api/v1/analyst-claims")
    def analyst_claims_route(limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return analyst_claims(database, limit, offset)

    @router.get("/api/v1/analyst-factors")
    def analyst_factors(as_of_date: date | None = None, lookback_days: int = 7) -> dict[str, Any]:
        with database.transaction() as connection:
            return factor_summary_fn(connection, as_of_date or datetime.now(ZoneInfo("Asia/Shanghai")).date(), lookback_days)

    @router.get("/api/v1/claim-review")
    def claim_review_queue_route(status: Literal["pending", "approved", "rejected"] = "pending", limit: int = 100) -> dict[str, Any]:
        return claim_review_queue(database, status, limit)

    return router
