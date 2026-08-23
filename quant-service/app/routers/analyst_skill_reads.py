"""Read-only analyst language-skill profiles."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_analyst_skill_read_repository import profiles as async_profiles


def build_analyst_skill_reads_router(
    database: Any,
    profiles_fn: Callable[[Any, str | None, int], dict[str, Any]],
    *,
    async_database: Any | None = None,
    async_profiles_fn: Callable[[Any, str | None, int], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["analyst-skill-reads"])

    @router.get("/api/v1/analyst-skills")
    async def profiles(analyst_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        if async_database is not None:
            return await (async_profiles_fn or async_profiles)(async_database, analyst_id, limit)
        return profiles_fn(database, analyst_id, limit)

    return router
