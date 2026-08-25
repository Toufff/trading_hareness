"""Dependency-explicit composition for durable leased runtime loops.

The module owns only lifecycle wiring.  Task cadence, task factories and
runtime-profile ownership stay with their dedicated configuration and registry
modules, so this boundary cannot introduce strategy or provider behaviour.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LeasedRuntimeDependencies:
    """Ports needed to bind a labelled loop to its durable DB lease."""

    database: Any
    lease_holder_id: Any
    lease_seconds: int
    acquire_lease: Callable[[Any, str, Any, int], Awaitable[bool]]
    renew_lease: Callable[[Any, str, Any, int], Awaitable[bool]]
    release_lease: Callable[[Any, str, Any], Awaitable[None]]
    supervise: Callable[..., Awaitable[None]]
    on_state: Callable[[str, str, str | None], None] | None = None


def build_leased_task_runner(
    dependencies: LeasedRuntimeDependencies,
) -> Callable[[str, Callable[[], Awaitable[None]]], Awaitable[None]]:
    """Return the adapter expected by ``start_leased_background_tasks``.

    Keeping this closure outside the ASGI composition root makes the database
    lease boundary independently testable and reusable by a preflight worker.
    """

    async def run_leased(label: str, factory: Callable[[], Awaitable[None]]) -> None:
        lease_key = f"background_loop:{label}"

        async def acquire() -> bool:
            return await dependencies.acquire_lease(
                dependencies.database,
                lease_key,
                dependencies.lease_holder_id,
                dependencies.lease_seconds,
            )

        async def renew() -> bool:
            return await dependencies.renew_lease(
                dependencies.database,
                lease_key,
                dependencies.lease_holder_id,
                dependencies.lease_seconds,
            )

        async def release() -> None:
            await dependencies.release_lease(
                dependencies.database,
                lease_key,
                dependencies.lease_holder_id,
            )

        await dependencies.supervise(
            label,
            factory,
            acquire,
            renew,
            release,
            dependencies.lease_seconds,
            on_state=dependencies.on_state,
        )

    return run_leased


__all__ = ["LeasedRuntimeDependencies", "build_leased_task_runner"]
