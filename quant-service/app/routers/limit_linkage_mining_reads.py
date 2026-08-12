"""Read-only route for live limit-up linkage research candidates."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..limit_linkage_mining_read_model import latest_limit_linkage_mining


def build_limit_linkage_mining_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["limit-linkage-mining-reads"])

    @router.get("/api/v1/intraday/limit-linkage/latest")
    def latest_limit_linkage(limit: int = 30) -> dict[str, Any]:
        return latest_limit_linkage_mining(database, limit)

    return router
