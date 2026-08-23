"""Bounded application startup and shutdown orchestration.

The service has several local resources with a required ordering: databases and
HTTP pools must be ready before catalog registration or leased loops start;
on shutdown, loops stop before their transports and databases disappear.  This
module owns that order without importing FastAPI, provider clients or globals,
so it can be exercised with local fakes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable


@dataclass(frozen=True)
class ApplicationLifecycleDependencies:
    open_database: Callable[[], Any]
    open_async_database: Callable[[], Awaitable[Any]]
    configure_request_reserver: Callable[..., Any]
    request_reserver: Callable[..., Awaitable[Any]]
    max_reservation_wait_seconds: float
    initialize_provider_metrics: Callable[[], Any]
    start_http_clients: Callable[[], Awaitable[Any]]
    legacy_schema_bootstrap_enabled: Callable[[], bool]
    migrate_database: Callable[[], Any]
    verify_versioned_schema: Callable[[], Any]
    ensure_catalog_capabilities: Callable[[], Any]
    run_database: Callable[..., Awaitable[Any]]
    start_background_tasks: Callable[[], dict[str, Any]]
    cancel_background_tasks: Callable[[dict[str, Any]], Awaitable[Any]]
    cancel_shared_snapshots: Callable[[], Awaitable[Any]]
    shutdown_super_get_executor: Callable[[], Any]
    shutdown_runtime_executors: Callable[[], Any]
    close_http_clients: Callable[[], Awaitable[Any]]
    close_async_database: Callable[[], Awaitable[Any]]
    close_database: Callable[[], Any]


@asynccontextmanager
async def application_lifespan(
    dependencies: ApplicationLifecycleDependencies,
) -> AsyncIterator[None]:
    """Start and stop local runtime resources in their required order.

    Catalog registration is explicitly submitted to the bounded database
    executor.  The function never performs a market request itself; provider
    work begins only inside the separately leased background loops.
    """
    database_open = False
    async_database_open = False
    reserver_configured = False
    http_clients_started = False
    background_tasks: dict[str, Any] | None = None
    try:
        dependencies.open_database()
        database_open = True
        await dependencies.open_async_database()
        async_database_open = True
        dependencies.configure_request_reserver(
            dependencies.request_reserver,
            max_wait_seconds=dependencies.max_reservation_wait_seconds,
        )
        reserver_configured = True
        dependencies.initialize_provider_metrics()
        await dependencies.start_http_clients()
        http_clients_started = True
        if dependencies.legacy_schema_bootstrap_enabled():
            dependencies.migrate_database()
        dependencies.verify_versioned_schema()
        await dependencies.run_database(dependencies.ensure_catalog_capabilities, timeout_seconds=30)
        background_tasks = dependencies.start_background_tasks()
        yield
    finally:
        if background_tasks is not None:
            await dependencies.cancel_background_tasks(background_tasks)
            await dependencies.cancel_shared_snapshots()
        if async_database_open:
            dependencies.shutdown_super_get_executor()
            dependencies.shutdown_runtime_executors()
        if http_clients_started:
            await dependencies.close_http_clients()
        if reserver_configured:
            dependencies.configure_request_reserver(None)
        if async_database_open:
            await dependencies.close_async_database()
        if database_open:
            dependencies.close_database()


__all__ = ["ApplicationLifecycleDependencies", "application_lifespan"]
