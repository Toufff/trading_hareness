"""Operational health, metrics and legacy-bootstrap route assembly.

These endpoints deliberately have no market-provider dependency.  Keeping
their HTTP declarations outside the application composition root makes that
boundary explicit while leaving the root responsible for injecting local
runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..logging_config import get_logger
from ..runtime_executors import run_database_blocking


logger = get_logger(__name__)


@dataclass(frozen=True)
class SystemControlDependencies:
    health_payload: Callable[[], dict[str, Any]]
    database_unavailable_error: type[Exception]
    metrics_response: Callable[[], Response]


def build_system_control_router(deps: SystemControlDependencies) -> APIRouter:
    """Expose only local operational controls with injected behavior."""
    router = APIRouter(tags=["system-control"])

    @router.get("/health")
    async def health() -> dict[str, Any]:
        try:
            # Docker's healthcheck polls this every ~15s; running it through
            # the bounded fast lane instead of anyio's unbounded default
            # threadpool keeps a stuck query from silently accumulating one
            # more occupied worker thread per poll.
            return await run_database_blocking(deps.health_payload, timeout_seconds=3)
        except deps.database_unavailable_error as error:
            # The raw driver error can carry host/port/user details; log it
            # locally and return only a fixed, unauthenticated-safe message.
            logger.warning(f"health check: database unavailable: {error}", extra={"task": "health"})
            raise HTTPException(status_code=503, detail="database unavailable") from error

    @router.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return deps.metrics_response()

    return router


__all__ = ["SystemControlDependencies", "build_system_control_router"]
