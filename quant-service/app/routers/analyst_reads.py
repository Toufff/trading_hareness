"""Read-only routes for already extracted analyst text evidence."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from ..async_analyst_archive_read_repository import analyst_claims as async_analyst_claims
from ..async_analyst_archive_read_repository import analyst_global_sync_cursor as async_global_sync_cursor
from ..async_analyst_archive_read_repository import analyst_sync_cursor as async_sync_cursor
from ..async_analyst_archive_read_repository import claim_review_queue as async_claim_review_queue
from ..async_analyst_archive_read_repository import remote_messages as async_remote_messages
from ..async_analyst_archive_read_repository import remote_report_list_state as async_remote_archive_state
from ..async_analyst_text_feature_read_repository import analyst_text_factor_summary as async_analyst_factor_summary
from ..async_analyst_archive_read_repository import remote_reports as async_remote_reports
from ..analyst_read_model import analyst_claims, claim_review_queue, remote_messages, remote_reports
from ..remote_archive import analyst_global_sync_cursor, analyst_sync_cursor
from ..runtime_executors import run_database_blocking


def build_analyst_reads_router(
    database: Any,
    remote_state_fn: Callable[[Any], dict[str, Any]],
    factor_summary_fn: Callable[[Any, date, int], dict[str, Any]],
    *,
    async_database: Any | None = None,
    async_remote_reports_fn: Callable[[Any, int, int], Awaitable[dict[str, Any]]] | None = None,
    async_remote_messages_fn: Callable[[Any, str | None, int, int], Awaitable[dict[str, Any]]] | None = None,
    async_analyst_claims_fn: Callable[[Any, int, int], Awaitable[dict[str, Any]]] | None = None,
    async_claim_review_queue_fn: Callable[[Any, str, int], Awaitable[dict[str, Any]]] | None = None,
    async_remote_archive_state_fn: Callable[[Any], Awaitable[dict[str, Any]]] | None = None,
    async_sync_cursor_fn: Callable[[Any, str, str], Awaitable[dict[str, Any]]] | None = None,
    async_global_sync_cursor_fn: Callable[[Any, str], Awaitable[dict[str, Any]]] | None = None,
    async_factor_summary_fn: Callable[[Any, date, int], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["analyst-reads"])

    def analyst_factors_sync(target: date, lookback_days: int) -> dict[str, Any]:
        with database.transaction() as connection:
            return factor_summary_fn(connection, target, lookback_days)

    @router.get("/api/v1/remote-archive/state")
    async def remote_archive_state() -> dict[str, Any]:
        if async_database is not None:
            return await (async_remote_archive_state_fn or async_remote_archive_state)(async_database)
        return remote_state_fn(database)

    @router.get("/api/v1/remote-archive/reports")
    async def remote_archive_reports(limit: int = 30, offset: int = 0) -> dict[str, Any]:
        if async_database is not None:
            return await (async_remote_reports_fn or async_remote_reports)(async_database, limit, offset)
        return remote_reports(database, limit, offset)

    @router.get("/api/v1/remote-archive/messages")
    async def remote_archive_messages(analyst_id: str | None = None, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        if async_database is not None:
            return await (async_remote_messages_fn or async_remote_messages)(async_database, analyst_id, limit, offset)
        return remote_messages(database, analyst_id, limit, offset)

    @router.get("/api/v1/remote-archive/sync-cursors/{stream_key}/{analyst_id}")
    async def sync_cursor(stream_key: str, analyst_id: str) -> dict[str, Any]:
        if async_database is not None:
            return await (async_sync_cursor_fn or async_sync_cursor)(async_database, stream_key, analyst_id)
        return analyst_sync_cursor(database, stream_key, analyst_id)

    @router.get("/api/v1/remote-archive/sync-cursors-global/{stream_key}")
    async def global_sync_cursor(stream_key: str) -> dict[str, Any]:
        if async_database is not None:
            return await (async_global_sync_cursor_fn or async_global_sync_cursor)(async_database, stream_key)
        return analyst_global_sync_cursor(database, stream_key)

    @router.get("/api/v1/analyst-claims")
    async def analyst_claims_route(limit: int = 100, offset: int = 0) -> dict[str, Any]:
        if async_database is not None:
            return await (async_analyst_claims_fn or async_analyst_claims)(async_database, limit, offset)
        return analyst_claims(database, limit, offset)

    @router.get("/api/v1/analyst-factors")
    async def analyst_factors(as_of_date: date | None = None, lookback_days: int = 7) -> dict[str, Any]:
        target = as_of_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if async_database is not None:
            return await (async_factor_summary_fn or async_analyst_factor_summary)(async_database, target, lookback_days)
        return await run_database_blocking(analyst_factors_sync, target, lookback_days)

    @router.get("/api/v1/claim-review")
    async def claim_review_queue_route(status: Literal["pending", "approved", "rejected"] = "pending", limit: int = 100) -> dict[str, Any]:
        if async_database is not None:
            return await (async_claim_review_queue_fn or async_claim_review_queue)(async_database, status, limit)
        return claim_review_queue(database, status, limit)

    return router
