"""Read-only provider catalog and health routes.

The router accepts its dependencies at assembly time, keeping imports free of
the application singleton and making the no-poll/no-write contract explicit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from ..async_provider_status_read_repository import provider_capabilities as async_provider_capabilities
from ..async_provider_status_read_repository import provider_health as async_provider_health
from ..async_provider_status_read_repository import tushare_catalog as async_tushare_catalog
from ..provider_catalog import provider_capabilities_snapshot, tushare_catalog_snapshot
from ..provider_observability import provider_health_snapshot


def build_provider_status_router(
    database: Any,
    provider_status_fn: Callable[[], list[dict[str, Any]]],
    free_provider_status_fn: Callable[[], list[dict[str, Any]]],
    *,
    async_database: Any | None = None,
    async_tushare_catalog_fn: Callable[[Any, Callable[[], list[dict[str, Any]]], Callable[[], list[dict[str, Any]]]], Awaitable[dict[str, Any]]] | None = None,
    async_provider_capabilities_fn: Callable[[Any], Awaitable[dict[str, Any]]] | None = None,
    async_provider_health_fn: Callable[[Any, list[dict[str, Any]], datetime], Awaitable[dict[str, Any]]] | None = None,
) -> APIRouter:
    """Build read-only routes without allowing a front-end refresh to probe."""
    router = APIRouter(tags=["provider-status"])

    @router.get("/api/v1/providers/tushare/catalog")
    async def tushare_catalog() -> dict[str, Any]:
        if async_database is not None:
            return await (async_tushare_catalog_fn or async_tushare_catalog)(
                async_database, provider_status_fn, free_provider_status_fn,
            )
        return tushare_catalog_snapshot(database)

    @router.get("/api/v1/providers/capabilities")
    async def provider_api_capabilities() -> dict[str, Any]:
        if async_database is not None:
            return await (async_provider_capabilities_fn or async_provider_capabilities)(async_database)
        return provider_capabilities_snapshot(database)

    @router.get("/api/v1/providers/health")
    async def providers_health() -> dict[str, Any]:
        provider_configs = [*provider_status_fn(), *free_provider_status_fn()]
        observed_at = datetime.now(timezone.utc)
        if async_database is not None:
            return await (async_provider_health_fn or async_provider_health)(async_database, provider_configs, observed_at)
        return provider_health_snapshot(
            database,
            provider_configs, observed_at,
        )

    return router
