"""Bounded public-source probes used by the single-stock research service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .runtime_executors import ExecutorSaturatedError


@dataclass(frozen=True)
class StockStudyPublicDependencies:
    open_provider_capabilities: Callable[[str, list[str]], Awaitable[set[str]]]
    run_database: Callable[..., Awaitable[Any]]
    persist_success: Callable[..., int]
    persist_failure: Callable[..., None]
    safe_error_detail: Callable[[str, int], str]
    request_errors: tuple[type[Exception], ...]


async def fetch(
    label: str,
    provider: str,
    capability: str,
    fetcher: Callable[[], Awaitable[Any]],
    symbol: str,
    deps: StockStudyPublicDependencies,
) -> tuple[dict[str, Any], Any]:
    """Fetch and persist one public study source without creating work on an open circuit."""
    if capability in await deps.open_provider_capabilities(provider, [capability]):
        return (
            {"source": label, "api_name": capability, "provider": provider, "status": "circuit_open",
             "received": 0, "stored": 0, "error": "provider health circuit is open; upstream request skipped"},
            [] if capability == "daily_bar" else None,
        )
    try:
        started_at = asyncio.get_running_loop().time()
        # The factory is not invoked until after the durable circuit check, so
        # a circuit-open state never allocates an HTTP coroutine or request.
        payload = await asyncio.wait_for(fetcher(), timeout=10)
        received = len(payload) if isinstance(payload, list) else int(bool(payload))
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        stored = await deps.run_database(
            deps.persist_success, provider, capability, payload, symbol, latency_ms, timeout_seconds=60,
        )
        return (
            {"source": label, "api_name": capability, "provider": provider,
             "status": "completed" if received else "empty", "received": received, "stored": stored},
            payload,
        )
    except ExecutorSaturatedError as error:
        return (
            {"source": label, "api_name": capability, "provider": provider, "status": "blocked",
             "received": 0, "stored": 0, "error": deps.safe_error_detail(str(error), 300)},
            [] if capability == "daily_bar" else None,
        )
    except Exception as error:  # provider error types are injected by the composition root
        if not isinstance(error, (asyncio.TimeoutError, *deps.request_errors)):
            raise
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        await deps.run_database(
            deps.persist_failure, provider, capability,
            str(error) or "public provider request timed out", latency_ms,
        )
        return (
            {"source": label, "api_name": capability, "provider": provider, "status": "failed",
             "received": 0, "stored": 0, "error": str(error) or "public provider request timed out"},
            [] if capability == "daily_bar" else None,
        )


__all__ = ["StockStudyPublicDependencies", "fetch"]
