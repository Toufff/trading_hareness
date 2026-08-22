"""Read-only automation and agent-maintenance projections."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..agent_context import repository_agent_context
from ..automation_run_repository import latest_runs


def build_automation_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["automation-reads"])

    @router.get("/api/v1/agent/context")
    def agent_context() -> dict[str, Any]:
        return repository_agent_context()

    @router.get("/api/v1/automation/runs")
    def automation_runs(task_key: str | None = None, limit: int = 20) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        with database.transaction() as connection:
            items = latest_runs(connection, task_key=task_key, limit=bounded)
        return {
            "items": items,
            "context_version": repository_agent_context()["context_version"],
            "live_effect": "none",
        }

    return router


__all__ = ["build_automation_reads_router"]
