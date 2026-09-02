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
import sys
from typing import Any, AsyncIterator, Awaitable, Callable

from .logging_config import configure_logging


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
    verify_strategy_contracts: Callable[[], Any] = lambda: None
    # Resolved once at startup so the app-wide write-key middleware never
    # reads the environment per request; must raise to fail closed when no
    # write key is configured (see ``app.main.resolve_write_api_key``).
    resolve_write_api_key: Callable[[], Any] = lambda: None
    # Peer/edge deployments run read-only against a shared control plane and
    # must not attempt to register or mutate catalog capabilities there.
    control_plane_writes_enabled: Callable[[], bool] = lambda: True


async def _run_cleanup_steps(
    steps: list[tuple[bool, Callable[[], Any]]],
    *,
    preserve_active_exception: bool,
) -> None:
    """Run every applicable shutdown step even if one local cleanup fails.

    A failed task cancellation must not leave the HTTP pools or database open.
    If application startup/request handling is already failing, its original
    exception remains authoritative; on an otherwise normal shutdown the
    first cleanup failure is reported after the remaining steps have run.
    """
    failures: list[Exception] = []
    for enabled, callback in steps:
        if not enabled:
            continue
        try:
            result = callback()
            if hasattr(result, "__await__"):
                await result
        except Exception as error:  # noqa: BLE001 - cleanup must continue for later owned resources
            failures.append(error)
    if failures and not preserve_active_exception:
        raise failures[0]


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
    # Structured logging has no external resource of its own (no socket, no
    # file handle beyond stdout) and every later step's failure should already
    # be logged, so it is configured unconditionally before anything else.
    configure_logging()
    try:
        # Fail closed before touching any resource: a misconfigured deployment
        # with no write key must refuse to start rather than run with every
        # write request silently allowed.
        dependencies.resolve_write_api_key()
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
        if dependencies.control_plane_writes_enabled():
            await dependencies.run_database(dependencies.ensure_catalog_capabilities, timeout_seconds=30)
        dependencies.verify_strategy_contracts()
        background_tasks = dependencies.start_background_tasks()
        yield
    finally:
        await _run_cleanup_steps([
            (background_tasks is not None, lambda: dependencies.cancel_background_tasks(background_tasks or {})),
            (background_tasks is not None, dependencies.cancel_shared_snapshots),
            # These thread pools are process-local and created at import time;
            # they own no async database connection. Gating them on
            # ``async_database_open`` meant a failure between the synchronous
            # and async database opens skipped their shutdown even though the
            # synchronous blocking executor could already have accepted work.
            # ``database_open`` is the earliest point either executor could
            # plausibly be in use.
            (database_open, dependencies.shutdown_super_get_executor),
            (database_open, dependencies.shutdown_runtime_executors),
            (http_clients_started, dependencies.close_http_clients),
            (reserver_configured, lambda: dependencies.configure_request_reserver(None)),
            (async_database_open, dependencies.close_async_database),
            (database_open, dependencies.close_database),
        ], preserve_active_exception=sys.exc_info()[0] is not None)


__all__ = ["ApplicationLifecycleDependencies", "application_lifespan"]
