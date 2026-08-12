"""Small supervision helpers for bounded background asyncio tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Awaitable

from .telemetry import background_loop_restarts_total


def observe_completed_task(task: asyncio.Task[Any], in_flight: set[asyncio.Task[Any]], label: str) -> None:
    """Remove a task and consume any exception so it cannot become unobserved."""
    in_flight.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as error:  # noqa: BLE001 - surface a bounded loop failure without crashing its scheduler
        print(f"{label} task failed: {str(error)[:300]}")


async def supervise_loop(label: str, factory: Callable[[], Awaitable[None]], restart_delay_seconds: float = 5.0) -> None:
    """Restart an unexpectedly exited long-running loop, but never swallow cancellation."""
    # Materialize the zero-value sample at startup so dashboards can distinguish
    # a healthy loop with zero restarts from a metric that was never registered.
    background_loop_restarts_total.labels(label)
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - one loop must not silently die for the process lifetime
            background_loop_restarts_total.labels(label).inc()
            print(f"{label} loop failed; restarting: {str(error)[:300]}")
        else:
            background_loop_restarts_total.labels(label).inc()
            print(f"{label} loop exited unexpectedly; restarting")
        await asyncio.sleep(max(0.1, restart_delay_seconds))


async def supervise_leased_loop(
    label: str,
    factory: Callable[[], Awaitable[None]],
    acquire: Callable[[], Awaitable[bool]],
    renew: Callable[[], Awaitable[bool]],
    release: Callable[[], Awaitable[None]],
    lease_seconds: int,
    retry_delay_seconds: float = 5.0,
) -> None:
    """Run one supervised loop only while this process owns its durable lease."""
    renew_delay = max(1.0, lease_seconds / 3)
    while True:
        try:
            owns_lease = await acquire()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - DB control-plane outage must not kill the loop task
            print(f"{label} lease acquire failed; retrying: {str(error)[:300]}")
            await asyncio.sleep(max(0.1, retry_delay_seconds))
            continue
        if not owns_lease:
            await asyncio.sleep(max(0.1, retry_delay_seconds))
            continue
        worker = asyncio.create_task(supervise_loop(label, factory, restart_delay_seconds=retry_delay_seconds))

        async def renew_until_lost() -> None:
            while True:
                await asyncio.sleep(renew_delay)
                try:
                    renewed = await renew()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - conservatively stop before an unverified lease expires
                    print(f"{label} lease renew failed; treating lease as lost: {str(error)[:300]}")
                    return
                if not renewed:
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
                print(f"{label} lease release failed; it will expire naturally: {str(error)[:300]}")
        await asyncio.sleep(max(0.1, retry_delay_seconds))
