"""Read-only agent paper account status and human comparison."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException

from ..agent_paper.report import status as agent_paper_status
from ..runtime_executors import run_database_blocking


def build_agent_paper_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["agent-paper"])

    @router.get("/api/v1/agent-paper/status")
    async def status(account_key: str = "agent-claude-opus", day: str | None = None) -> dict[str, Any]:
        try:
            selected = date.fromisoformat(day) if day else None
        except ValueError as error:
            raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD") from error

        def run() -> dict[str, Any]:
            with database.transaction() as connection:
                return agent_paper_status(connection, account_key=account_key, day=selected)

        return await run_database_blocking(run, timeout_seconds=10)

    return router


__all__ = ["build_agent_paper_reads_router"]
