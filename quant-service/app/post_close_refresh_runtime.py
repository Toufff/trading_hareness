"""HTTP-disconnect-safe ownership for one post-close refresh invocation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class PostCloseRefreshRuntime:
    """Keep a durable refresh alive when its requesting HTTP client disconnects.

    The refresh itself owns idempotency receipts and its cross-process lease.
    This adapter only detaches that durable work from an HTTP response task so
    a reverse proxy timeout cannot interrupt its ``finally`` release path.
    """

    def __init__(self) -> None:
        self._active: set[asyncio.Task[Any]] = set()

    async def run(self, action: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        task = asyncio.create_task(action(), name="post-close-refresh")
        self._active.add(task)
        task.add_done_callback(self._active.discard)
        return await asyncio.shield(task)

    @property
    def active_count(self) -> int:
        """Expose local in-flight ownership for health/tests without data access."""
        return len(self._active)


__all__ = ["PostCloseRefreshRuntime"]
