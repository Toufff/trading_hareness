"""Read-only analyst language-skill profiles."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter


def build_analyst_skill_reads_router(database: Any, profiles_fn: Callable[[Any, str | None, int], dict[str, Any]]) -> APIRouter:
    router = APIRouter(tags=["analyst-skill-reads"])

    @router.get("/api/v1/analyst-skills")
    def profiles(analyst_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        return profiles_fn(database, analyst_id, limit)

    return router
