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


@dataclass(frozen=True)
class SystemControlDependencies:
    health_payload: Callable[[], dict[str, Any]]
    database_unavailable_error: type[Exception]
    metrics_response: Callable[[], Response]
    legacy_bootstrap: Callable[[], dict[str, Any]]


def build_system_control_router(deps: SystemControlDependencies) -> APIRouter:
    """Expose only local operational controls with injected behavior."""
    router = APIRouter(tags=["system-control"])

    @router.get("/health")
    def health() -> dict[str, Any]:
        try:
            return deps.health_payload()
        except deps.database_unavailable_error as error:
            raise HTTPException(status_code=503, detail=f"database unavailable: {error}") from error

    @router.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return deps.metrics_response()

    @router.post("/api/v1/bootstrap")
    def bootstrap() -> dict[str, Any]:
        return deps.legacy_bootstrap()

    return router


__all__ = ["SystemControlDependencies", "build_system_control_router"]
