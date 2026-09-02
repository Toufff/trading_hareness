"""Dependency-explicit composition for durable leased runtime loops.

The module owns only lifecycle wiring.  Task cadence, task factories and
runtime-profile ownership stay with their dedicated configuration and registry
modules, so this boundary cannot introduce strategy or provider behaviour.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Fence captured at the most recent successful lease acquisition for each
# labelled loop, keyed by label.  A plain module-level dict is used instead
# of a ``contextvars.ContextVar`` because the write path this feeds
# eventually runs inside a ``ThreadPoolExecutor`` worker (see
# ``runtime_executors.py``'s ``run_in_executor`` usage), and asyncio does not
# propagate ``contextvars`` context across that thread-pool boundary. Simple
# dict get/set is atomic under the GIL, which is sufficient here: only the
# owning loop's own ``acquire()`` writes a label's entry.
_LEASE_FENCES: dict[str, int | None] = {}


def lease_key_for_label(label: str) -> str:
    """Return the durable lease key used by one labelled background loop."""
    return f"background_loop:{label}"


def current_lease_fence(label: str) -> int | None:
    """Return the fencing token captured at ``label``'s last successful acquire.

    ``None`` when this process has never (yet) acquired that label's lease,
    which callers should treat as "no fence to check" rather than "fence
    lost" (e.g. a manually-triggered write outside the leased loop).
    """
    return _LEASE_FENCES.get(label)


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
        lease_key = lease_key_for_label(label)

        async def acquire() -> bool:
            fence = await dependencies.acquire_lease(
                dependencies.database,
                lease_key,
                dependencies.lease_holder_id,
                dependencies.lease_seconds,
            )
            # Captured here (before the loop's worker task is created) so a
            # task body can look up "the fence this ownership epoch acquired"
            # via ``current_lease_fence(label)`` at write time.
            _LEASE_FENCES[label] = fence
            return bool(fence)

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


__all__ = [
    "LeasedRuntimeDependencies",
    "build_leased_task_runner",
    "current_lease_fence",
    "lease_key_for_label",
]
