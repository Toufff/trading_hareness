"""Read-only runtime-status route assembly."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter


def build_intraday_status_router(status_payload_fn: Callable[[], dict[str, Any]]) -> APIRouter:
    """Keep the public status URL independent from the application singleton."""
    router = APIRouter(tags=["intraday-status"])

    @router.get("/api/v1/intraday/services/status")
    def intraday_services_status() -> dict[str, Any]:
        return status_payload_fn()

    return router
