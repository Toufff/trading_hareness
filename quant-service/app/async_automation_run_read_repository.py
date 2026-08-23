"""Native-async projection of durable automation run receipts."""

from __future__ import annotations

from typing import Any

from .automation_run_repository import LATEST_RUNS_SQL, latest_runs_params


async def latest_runs(async_database: Any, task_key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    params, _ = latest_runs_params(task_key, limit)
    async with async_database.transaction() as connection:
        result = await connection.execute(LATEST_RUNS_SQL, params)
        return [dict(row) for row in await result.fetchall()]


__all__ = ["latest_runs"]
