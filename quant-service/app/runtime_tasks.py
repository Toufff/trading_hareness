"""Small supervision helpers for bounded background asyncio tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from threading import RLock
from typing import Any, Awaitable, Mapping

from .telemetry import background_loop_restarts_total
from .network_health import network_state
from .platform.runtime_task_registry import (
    RUNTIME_TASK_CONTRACTS,
    intraday_edge_task_labels,
    runtime_profile_owns_task,
)


# Compatibility export for existing callers. The registry is now the source
# of truth, so ownership is not duplicated in a profile filter and a separate
# documentation list.
INTRADAY_EDGE_BACKGROUND_LOOPS = intraday_edge_task_labels()
BACKGROUND_RUNTIME_PROFILES = frozenset({"full", "research", "intraday_edge"})


@dataclass(frozen=True)
class BackgroundTaskSpec:
    """One optional leased loop owned by the application lifespan."""

    label: str
    enabled: bool
    factory: Callable[[], Awaitable[None]]


def background_tasks_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this process may acquire any background-loop lease.

    A preflight instance can validate its image, schema and HTTP pools against
    production state without competing for collection or strategy work. The
    normal service remains enabled by default.
    """
    values = environ if environ is not None else os.environ
    return str(values.get("QUANT_BACKGROUND_TASKS_ENABLED", "true")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def background_runtime_profile(environ: Mapping[str, str] | None = None) -> str:
    """Resolve one explicit single-writer role for process-owned loops."""
    values = environ if environ is not None else os.environ
    profile = str(values.get("QUANT_RUNTIME_PROFILE", "full")).strip().lower() or "full"
    if profile not in BACKGROUND_RUNTIME_PROFILES:
        supported = ", ".join(sorted(BACKGROUND_RUNTIME_PROFILES))
        raise ValueError(f"QUANT_RUNTIME_PROFILE must be one of: {supported}")
    return profile


def apply_background_runtime_profile(
    specs: tuple[BackgroundTaskSpec, ...],
    environ: Mapping[str, str] | None = None,
) -> tuple[BackgroundTaskSpec, ...]:
    """Disable loops not owned by this process without changing their config.

    ``intraday_edge`` is the always-on network collector and alert owner.
    ``research`` owns everything else and cannot acquire a live collection
    lease even when a legacy per-loop flag remains enabled. ``full`` preserves
    the existing single-process behaviour for development and recovery.
    """
    profile = background_runtime_profile(environ)
    if profile == "full":
        return specs
    return tuple(
        BackgroundTaskSpec(
            spec.label,
            spec.enabled and runtime_profile_owns_task(profile, spec.label),
            spec.factory,
        )
        for spec in specs
    )


def validate_runtime_task_specs(specs: tuple[BackgroundTaskSpec, ...]) -> None:
    """Fail startup if the composition root drifts from declared task ownership.

    This guard intentionally runs before a runtime profile disables entries:
    every deployable task must remain documented even when this process owns
    only a subset of them.
    """
    configured = {spec.label for spec in specs}
    declared = set(RUNTIME_TASK_CONTRACTS)
    undeclared = sorted(configured - declared)
    missing = sorted(declared - configured)
    if undeclared or missing:
        details = []
        if undeclared:
            details.append(f"undeclared labels: {', '.join(undeclared)}")
        if missing:
            details.append(f"missing declared labels: {', '.join(missing)}")
        raise ValueError("runtime task specifications drift from registry; " + "; ".join(details))


def start_leased_background_tasks(
    specs: tuple[BackgroundTaskSpec, ...],
    run_leased: Callable[[str, Callable[[], Awaitable[None]]], Awaitable[None]],
) -> dict[str, asyncio.Task[None]]:
    """Start each enabled loop once and retain its task by durable lease label."""
    labels = [spec.label for spec in specs]
    if len(labels) != len(set(labels)):
        raise ValueError("background task labels must be unique")
    return {
        spec.label: asyncio.create_task(
            run_leased(spec.label, spec.factory), name=f"background-loop:{spec.label}",
        )
        for spec in specs
        if spec.enabled
    }


async def cancel_background_tasks(tasks: dict[str, asyncio.Task[None]]) -> None:
    """Cancel and await every owned loop so shutdown cannot leave orphan tasks."""
    for task in tasks.values():
        task.cancel()
    for task in tasks.values():
        try:
            await task
        except asyncio.CancelledError:
            pass


class LoopRuntimeRegistry:
    """Process-local explanation of each leased loop's current lifecycle state."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._items: dict[str, dict[str, Any]] = {}

    def mark(self, label: str, state: str, error: str | None = None) -> None:
        with self._lock:
            item = dict(self._items.get(label) or {})
            item.update({"state": state, "updated_at": datetime.now(timezone.utc).isoformat()})
            if error:
                item["last_error"] = str(error)[:300]
            elif state in {"running", "lease_owned", "standby", "waiting_for_lease"}:
                item["last_error"] = None
            self._items[label] = item

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {label: dict(item) for label, item in sorted(self._items.items())}


def observe_completed_task(task: asyncio.Task[Any], in_flight: set[asyncio.Task[Any]], label: str) -> None:
    """Remove a task and consume any exception so it cannot become unobserved."""
    in_flight.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as error:  # noqa: BLE001 - surface a bounded loop failure without crashing its scheduler
        print(f"{label} task failed: {str(error)[:300]}")


async def supervise_loop(label: str, factory: Callable[[], Awaitable[None]], restart_delay_seconds: float = 5.0,
                         on_state: Callable[[str, str, str | None], None] | None = None) -> None:
    """Restart an unexpectedly exited long-running loop, but never swallow cancellation."""
    # Materialize the zero-value sample at startup so dashboards can distinguish
    # a healthy loop with zero restarts from a metric that was never registered.
    background_loop_restarts_total.labels(label)
    failure_streak = 0
    while True:
        try:
            if on_state is not None:
                on_state(label, "running", None)
            await factory()
        except asyncio.CancelledError:
            if on_state is not None:
                on_state(label, "stopped", None)
            raise
        except Exception as error:  # noqa: BLE001 - one loop must not silently die for the process lifetime
            background_loop_restarts_total.labels(label).inc()
            failure_streak += 1
            if on_state is not None:
                on_state(label, "failed", str(error))
            print(f"{label} loop failed; restarting: {str(error)[:300]}")
        else:
            background_loop_restarts_total.labels(label).inc()
            failure_streak = 0
            if on_state is not None:
                on_state(label, "exited", None)
            print(f"{label} loop exited unexpectedly; restarting")
        network = network_state.snapshot()
        backoff = restart_delay_seconds * (2 ** min(max(failure_streak - 1, 0), 4))
        if network["state"] == "offline":
            backoff = max(backoff, 30.0)
        if on_state is not None:
            on_state(label, "backing_off", None)
        await asyncio.sleep(min(60.0, max(0.1, backoff)))


async def supervise_leased_loop(
    label: str,
    factory: Callable[[], Awaitable[None]],
    acquire: Callable[[], Awaitable[bool]],
    renew: Callable[[], Awaitable[bool]],
    release: Callable[[], Awaitable[None]],
    lease_seconds: int,
    retry_delay_seconds: float = 5.0,
    on_state: Callable[[str, str, str | None], None] | None = None,
) -> None:
    """Run one supervised loop only while this process owns its durable lease."""
    renew_delay = max(1.0, lease_seconds / 3)
    while True:
        if on_state is not None:
            on_state(label, "acquiring_lease", None)
        try:
            owns_lease = await acquire()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - DB control-plane outage must not kill the loop task
            if on_state is not None:
                on_state(label, "lease_acquire_failed", str(error))
            print(f"{label} lease acquire failed; retrying: {str(error)[:300]}")
            await asyncio.sleep(max(0.1, retry_delay_seconds))
            continue
        if not owns_lease:
            if on_state is not None:
                on_state(label, "waiting_for_lease", None)
            await asyncio.sleep(max(0.1, retry_delay_seconds))
            continue
        if on_state is not None:
            on_state(label, "lease_owned", None)
        worker = asyncio.create_task(supervise_loop(
            label, factory, restart_delay_seconds=retry_delay_seconds, on_state=on_state,
        ))

        async def renew_until_lost() -> None:
            while True:
                await asyncio.sleep(renew_delay)
                try:
                    renewed = await renew()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - conservatively stop before an unverified lease expires
                    if on_state is not None:
                        on_state(label, "lease_renew_failed", str(error))
                    print(f"{label} lease renew failed; treating lease as lost: {str(error)[:300]}")
                    return
                if not renewed:
                    if on_state is not None:
                        on_state(label, "lease_lost", None)
                    return

        renewal = asyncio.create_task(renew_until_lost())
        try:
            done, _ = await asyncio.wait((worker, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                if not worker.done():
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
                print(f"{label} lease lost; waiting for a new owner window")
            else:
                await worker
        finally:
            renewal.cancel()
            try:
                await renewal
            except asyncio.CancelledError:
                pass
            try:
                await release()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - expiry remains the safe takeover fallback
                if on_state is not None:
                    on_state(label, "lease_release_failed", str(error))
                print(f"{label} lease release failed; it will expire naturally: {str(error)[:300]}")
            else:
                if on_state is not None:
                    on_state(label, "standby", None)
        await asyncio.sleep(max(0.1, retry_delay_seconds))
