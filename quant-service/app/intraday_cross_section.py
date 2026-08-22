"""Shared, bounded cross-sectional snapshots for intraday research.

The all-A public quote request is deliberately slower and broader than the
explicit watchlist batch.  This tiny component deduplicates concurrent callers
and carries the true local receive age forward to the decision evidence.  It
does not decide whether an aged snapshot is eligible for a signal; that remains
the live policy's responsibility.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar


RowT = TypeVar("RowT")


@dataclass(frozen=True)
class SnapshotStatus:
    """Source provenance returned with one shared snapshot."""

    status: str
    age_seconds: float
    ttl_seconds: float

    def as_dict(self) -> dict[str, float | str]:
        return {
            "status": self.status,
            "age_seconds": round(max(0.0, self.age_seconds), 3),
            "ttl_seconds": self.ttl_seconds,
        }


class SharedAsyncSnapshot(Generic[RowT]):
    """Deduplicate one async request and retain its result for a short TTL.

    Failed requests intentionally leave the previous cache untouched and are
    never represented as a fresh result.  This makes a transient public-source
    fault visible to the caller instead of silently re-labelling stale data.
    """

    def __init__(self, fetch: Callable[[], Awaitable[RowT]], *, ttl_seconds: float,
                 clock: Callable[[], float]) -> None:
        self._fetch = fetch
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cached: tuple[float, RowT] | None = None
        self._inflight: asyncio.Task[RowT] | None = None

    async def get(self) -> tuple[RowT, dict[str, float | str]]:
        """Return a bounded-age cached value or join exactly one in-flight fetch."""
        now = self._clock()
        cached = self._cached
        if cached is not None and now - cached[0] <= self._ttl_seconds:
            return cached[1], SnapshotStatus("cached", now - cached[0], self._ttl_seconds).as_dict()

        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.create_task(self._fetch())
        task = self._inflight
        try:
            value = await asyncio.shield(task)
        finally:
            # Clear only the task this call joined.  A future version may add
            # invalidation; it must not clear a newer in-flight task here.
            if self._inflight is task and task.done():
                self._inflight = None
        received_at = self._clock()
        self._cached = (received_at, value)
        return value, SnapshotStatus("fresh", 0.0, self._ttl_seconds).as_dict()

    async def cancel_inflight(self) -> None:
        """Cancel a detached fetch during application shutdown.

        Callers may shield a broad snapshot past their request deadline so a
        later scan can reuse it.  The lifecycle still owns that task and must
        explicitly cancel it before closing executors and HTTP pools.
        """
        task = self._inflight
        if task is None or task.done():
            self._inflight = None
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._inflight is task:
            self._inflight = None


__all__ = ["SharedAsyncSnapshot", "SnapshotStatus"]
