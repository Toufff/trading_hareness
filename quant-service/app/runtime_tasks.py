"""Small supervision helpers for bounded background asyncio tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Awaitable

from .telemetry import background_loop_restarts_total
from .network_health import network_state


@dataclass(frozen=True)
class BackgroundTaskSpec:
    """One optional leased loop owned by the application lifespan."""

    label: str
    enabled: bool
    factory: Callable[[], Awaitable[None]]


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
