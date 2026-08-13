"""Read-only runtime-status route assembly."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter


def build_intraday_status_router(status_payload_fn: Callable[[], dict[str, Any]],
                                 async_status_payload_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None) -> APIRouter:
    """Keep the public status URL independent from the application singleton."""
    router = APIRouter(tags=["intraday-status"])

    if async_status_payload_fn is not None:
        @router.get("/api/v1/intraday/services/status")
        async def intraday_services_status() -> dict[str, Any]:
            return await async_status_payload_fn()
    else:
        @router.get("/api/v1/intraday/services/status")
        def intraday_services_status() -> dict[str, Any]:
            return status_payload_fn()

    return router
