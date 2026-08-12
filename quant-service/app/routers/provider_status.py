"""Read-only provider catalog and health routes.

The router accepts its dependencies at assembly time, keeping imports free of
the application singleton and making the no-poll/no-write contract explicit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter

from ..provider_catalog import provider_capabilities_snapshot, tushare_catalog_snapshot
from ..provider_observability import provider_health_snapshot


def build_provider_status_router(
    database: Any,
    provider_status_fn: Callable[[], list[dict[str, Any]]],
    free_provider_status_fn: Callable[[], list[dict[str, Any]]],
) -> APIRouter:
    """Build read-only routes without allowing a front-end refresh to probe."""
    router = APIRouter(tags=["provider-status"])

    @router.get("/api/v1/providers/tushare/catalog")
    def tushare_catalog() -> dict[str, Any]:
        return tushare_catalog_snapshot(database)

    @router.get("/api/v1/providers/capabilities")
    def provider_api_capabilities() -> dict[str, Any]:
        return provider_capabilities_snapshot(database)

    @router.get("/api/v1/providers/health")
    def providers_health() -> dict[str, Any]:
        return provider_health_snapshot(
            database,
            [*provider_status_fn(), *free_provider_status_fn()],
            datetime.now(timezone.utc),
        )

    return router
