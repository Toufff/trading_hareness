"""Bounded blocking-executor boundaries for the async research service.

Third-party market clients and the legacy synchronous psycopg repository cannot
be cancelled safely once their worker has started.  Keeping their pools here
makes the capacity boundary explicit, reusable and observable without coupling
it to FastAPI routes or strategy rules.
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .telemetry import bounded_executor_rejections_total, bounded_executor_tasks, db_blocking_tasks


def bounded_worker_count(name: str, default: int, maximum: int) -> int:
    """Read one bounded worker setting without allowing an invalid env to add load."""
    try:
        return min(maximum, max(1, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def bounded_queue_size(name: str, default: int, maximum: int = 64) -> int:
    """Read a non-negative queue allowance without permitting unbounded backlog."""
    try:
        return min(maximum, max(0, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


class ExecutorSaturatedError(RuntimeError):
    """The work was rejected before it could queue behind a slow blocking call."""


class BlockingExecutorBoundary:
    """Bound synchronous work by both workers and queued submissions.

    ``asyncio.wait_for`` cannot stop a started thread.  The permit therefore
    remains held until the worker truly returns, even if its caller times out.
    This prevents repeated provider timeouts from silently becoming an
    unbounded ``ThreadPoolExecutor`` backlog.
    """

    def __init__(self, name: str, workers: int, queue_capacity: int) -> None:
        self.name = name
        self.workers = workers
        self.queue_capacity = queue_capacity
        self._slots = threading.BoundedSemaphore(workers + queue_capacity)
        self._lock = threading.Lock()
        self._occupied = 0
        bounded_executor_tasks.labels(name, "capacity").set(workers)
        bounded_executor_tasks.labels(name, "queue_capacity").set(queue_capacity)
        bounded_executor_tasks.labels(name, "inflight").set(0)
        bounded_executor_tasks.labels(name, "occupied").set(0)

    def _acquire(self) -> None:
        if not self._slots.acquire(blocking=False):
            bounded_executor_rejections_total.labels(self.name).inc()
            raise ExecutorSaturatedError(f"{self.name} blocking executor is saturated")
        with self._lock:
            self._occupied += 1
            bounded_executor_tasks.labels(self.name, "occupied").set(self._occupied)

    def _release(self) -> None:
        with self._lock:
            self._occupied -= 1
            bounded_executor_tasks.labels(self.name, "occupied").set(self._occupied)
        self._slots.release()

    async def run(self, executor: ThreadPoolExecutor, action: Any, *args: Any, timeout_seconds: float) -> Any:
        self._acquire()

        def tracked_action() -> Any:
            bounded_executor_tasks.labels(self.name, "inflight").inc()
            if self.name == "database":
                db_blocking_tasks.labels("inflight").inc()
            try:
                return action(*args)
            finally:
                if self.name == "database":
                    db_blocking_tasks.labels("inflight").dec()
                bounded_executor_tasks.labels(self.name, "inflight").dec()
                self._release()

        try:
            future = asyncio.get_running_loop().run_in_executor(executor, tracked_action)
        except BaseException:
            self._release()
            raise
        try:
            # Shielding preserves the queued/running worker after the caller's
            # deadline.  Its completion callback consumes a later exception so
            # timeout callers cannot leave an unobserved-future warning behind.
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_seconds)
        except BaseException:
            def consume_late_exception(completed: asyncio.Future[Any]) -> None:
                if not completed.cancelled():
                    try:
                        completed.exception()
                    except BaseException:
                        pass
            future.add_done_callback(consume_late_exception)
            raise

    def status(self) -> dict[str, int]:
        with self._lock:
            occupied = self._occupied
        return {
            "workers": self.workers,
            "queue_capacity": self.queue_capacity,
            "occupied": occupied,
            "available_slots": self.workers + self.queue_capacity - occupied,
        }


public_source_executor_workers = bounded_worker_count("AKSHARE_MAX_WORKERS", 4, 12)
public_source_executor = ThreadPoolExecutor(
    max_workers=public_source_executor_workers,
    thread_name_prefix="akshare",
)
database_executor_workers = bounded_worker_count("QUANT_DB_BLOCKING_MAX_WORKERS", 4, 8)
database_executor = ThreadPoolExecutor(
    max_workers=database_executor_workers,
    thread_name_prefix="quant-db",
)

public_source_queue_capacity = bounded_queue_size("AKSHARE_MAX_QUEUE", 8)
database_executor_queue_capacity = bounded_queue_size("QUANT_DB_BLOCKING_MAX_QUEUE", 8)
public_source_boundary = BlockingExecutorBoundary("public_source", public_source_executor_workers, public_source_queue_capacity)
database_executor_boundary = BlockingExecutorBoundary("database", database_executor_workers, database_executor_queue_capacity)
db_blocking_tasks.labels("capacity").set(database_executor_workers)
db_blocking_tasks.labels("queue_capacity").set(database_executor_queue_capacity)
db_blocking_tasks.labels("inflight").set(0)


async def run_akshare_blocking(action: Any, *args: Any, timeout_seconds: float) -> Any:
    """Run a public-source client in the bounded executor.

    The metric tracks worker occupancy rather than coroutine wait time, so an
    upstream timeout remains visible until the underlying synchronous request
    actually returns.
    """
    return await public_source_boundary.run(public_source_executor, action, *args, timeout_seconds=timeout_seconds)


async def run_database_blocking(action: Any, *args: Any, timeout_seconds: float = 10) -> Any:
    """Run a legacy synchronous repository operation in a bounded executor."""
    return await database_executor_boundary.run(database_executor, action, *args, timeout_seconds=timeout_seconds)


def runtime_executor_status() -> dict[str, dict[str, int]]:
    """Expose only local capacity state; never starts market or DB work."""
    return {"public_source": public_source_boundary.status(), "database": database_executor_boundary.status()}


def shutdown_runtime_executors() -> None:
    """Stop accepting new work during application shutdown without blocking it."""
    public_source_executor.shutdown(wait=False, cancel_futures=True)
    database_executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "bounded_worker_count",
    "bounded_queue_size",
    "BlockingExecutorBoundary",
    "database_executor_workers",
    "ExecutorSaturatedError",
    "runtime_executor_status",
    "run_akshare_blocking",
    "run_database_blocking",
    "shutdown_runtime_executors",
]
