"""HTTP-disconnect-safe ownership for one post-close refresh invocation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import Context
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from .logging_config import get_logger


logger = get_logger(__name__)


class PostCloseRefreshRuntime:
    """Keep a durable refresh alive when its requesting HTTP client disconnects.

    The refresh itself owns idempotency receipts and its cross-process lease.
    This adapter only detaches that durable work from an HTTP response task so
    a reverse proxy timeout cannot interrupt its ``finally`` release path.
    """

    def __init__(self) -> None:
        self._active: dict[asyncio.Task[Any], datetime] = {}
        self._last_outcome: dict[str, Any] | None = None

    def _create_detached_task(self, action: Callable[[], Awaitable[dict[str, Any]]]) -> asyncio.Task[Any]:
        """Escape the request-scoped cancellation context used by ASGI middleware."""
        return asyncio.create_task(action(), name="post-close-refresh", context=Context())

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """Retrieve the task's exception exactly once, from every completion path.

        ``start()``'s detached task previously never had its exception
        retrieved: a routine 409 lease conflict (another instance already
        running the refresh) surfaced only as an unobserved "Task exception
        was never retrieved" warning instead of a clear terminal status.
        """
        self._active.pop(task, None)
        if task.cancelled():
            self._last_outcome = {"status": "cancelled"}
            return
        error = task.exception()
        if error is None:
            self._last_outcome = {"status": "completed"}
            return
        if isinstance(error, HTTPException) and error.status_code == 409:
            # Another service instance already holds the durable post-close
            # lease. Expected under concurrent triggers, not a failure.
            self._last_outcome = {"status": "lease_conflict", "detail": str(error.detail)}
            return
        self._last_outcome = {"status": "failed", "error": str(error)[:300]}
        logger.error(
            f"detached post-close refresh task failed: {str(error)[:300]}",
            extra={"task": "post_close_refresh"},
        )

    def start(self, action: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """Detach one refresh from the initiating HTTP request.

        A caller can poll ``/health`` and durable automation receipts instead
        of holding a reverse-proxy connection open for provider or settlement
        work.  The post-close lease remains the cross-process authority.
        """
        if self._active:
            return {"status": "running", "already_running": True, **self.status()}
        task = self._create_detached_task(action)
        self._active[task] = datetime.now(timezone.utc)
        task.add_done_callback(self._on_task_done)
        return {"status": "running", "already_running": False, **self.status()}

    async def run(self, action: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        task = self._create_detached_task(action)
        self._active[task] = datetime.now(timezone.utc)
        task.add_done_callback(self._on_task_done)
        return await asyncio.shield(task)

    @property
    def active_count(self) -> int:
        """Expose local in-flight ownership for health/tests without data access."""
        return len(self._active)

    def status(self) -> dict[str, Any]:
        """Return process-local activity, distinct from the durable DB lease."""
        started = min(self._active.values(), default=None)
        return {
            "active_count": len(self._active),
            "oldest_started_at": started.isoformat() if started else None,
            "last_outcome": self._last_outcome,
        }


__all__ = ["PostCloseRefreshRuntime"]
