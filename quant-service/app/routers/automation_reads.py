"""Read-only automation and agent-maintenance projections."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_automation_run_read_repository import latest_runs as async_latest_runs
from ..agent_context import repository_agent_context
from ..automation_run_repository import latest_runs


def _automation_runs_sync(database: Any, task_key: str | None, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        items = latest_runs(connection, task_key=task_key, limit=limit)
    return {"items": items, "context_version": repository_agent_context()["context_version"], "live_effect": "none"}


def build_automation_reads_router(
    database: Any,
    *,
    async_database: Any | None = None,
    async_latest_runs_fn: Callable[[Any, str | None, int], Awaitable[list[dict[str, Any]]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["automation-reads"])

    @router.get("/api/v1/agent/context")
    def agent_context() -> dict[str, Any]:
        return repository_agent_context()

    @router.get("/api/v1/automation/runs")
    async def automation_runs(task_key: str | None = None, limit: int = 20) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        if async_database is not None:
            items = await (async_latest_runs_fn or async_latest_runs)(async_database, task_key, bounded)
            return {"items": items, "context_version": repository_agent_context()["context_version"], "live_effect": "none"}
        return _automation_runs_sync(database, task_key, bounded)

    return router


__all__ = ["build_automation_reads_router"]
