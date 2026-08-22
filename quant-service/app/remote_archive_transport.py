"""Bounded HTTP transport for the remote analyst text archive."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from time import monotonic
from typing import Any, Awaitable, Callable

import httpx
from fastapi import HTTPException

from .network_health import network_state


class RemoteArchiveTransport:
    """Serialize archive requests and apply bounded upstream retries.

    The archive quota is shared across catalog, update and detail routes.  A
    single keep-alive client plus this process-local gate prevents a report
    catalog fanout from starving the message stream.  The caller owns the
    settings and sleep function so the production entry point remains easy to
    test without making network calls.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_request_started = 0.0
        self._request_count = 0
        self._retry_count = 0
        self._status_counts: defaultdict[str, int] = defaultdict(int)

    def stats(self) -> dict[str, Any]:
        """Return token-free transport counters for sync observability."""
        return {
            "requests": self._request_count,
            "retries": self._retry_count,
            "status_counts": dict(self._status_counts),
        }

    async def get(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        settings: dict[str, Any],
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> dict[str, Any]:
        for attempt in range(4):
            async with self._lock:
                elapsed = monotonic() - self._last_request_started
                spacing = float(settings["request_interval_seconds"]) - elapsed
                if spacing > 0:
                    await sleep(spacing)
                self._last_request_started = monotonic()
                try:
                    response = await client.get(path, params=params)
                except (httpx.TimeoutException, httpx.TransportError) as error:
                    network_state.record_failure("remote_analyst_archive", str(error), transient=True)
                    if attempt < 3:
                        self._retry_count += 1
                        await sleep(min(30.0, 2.0 ** attempt))
                        continue
                    raise HTTPException(status_code=503, detail="remote analyst archive temporarily unreachable") from error
                self._request_count += 1
                self._status_counts[str(response.status_code)] += 1
                if 200 <= response.status_code < 400:
                    network_state.record_success("remote_analyst_archive")
                elif response.status_code in {408, 429, 500, 502, 503, 504}:
                    network_state.record_failure("remote_analyst_archive", f"HTTP {response.status_code}", transient=True)
            if not response.is_error:
                try:
                    payload = response.json()
                except ValueError as error:
                    raise HTTPException(status_code=502, detail="remote analyst archive returned invalid JSON") from error
                if not isinstance(payload, dict):
                    raise HTTPException(status_code=502, detail="remote analyst archive returned an invalid JSON envelope")
                return payload
            # 409 is a meaningful archive cursor-stale response. Preserve it
            # for the sync coordinator instead of wrapping it as a generic
            # 502; the coordinator can restart from its durable timestamp.
            if response.status_code == 409:
                detail = response.text.strip().replace("\n", " ")[:160]
                raise HTTPException(status_code=409, detail=f"remote analyst archive cursor stale: {detail or response.reason_phrase}")
            retryable = response.status_code in {429, 500, 502, 503, 504}
            if retryable and attempt < 3:
                self._retry_count += 1
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 2.0 ** attempt
                await sleep(min(30.0, max(1.0, delay)))
                continue
            detail = response.text.strip().replace("\n", " ")[:160]
            raise HTTPException(status_code=502, detail=f"remote analyst archive HTTP {response.status_code}: {detail or response.reason_phrase}")
        raise HTTPException(status_code=502, detail="remote analyst archive request exhausted retries")


__all__ = ["RemoteArchiveTransport"]
