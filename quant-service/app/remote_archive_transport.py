"""Bounded HTTP transport for the remote analyst text archive."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any, Awaitable, Callable

import httpx
from fastapi import HTTPException


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
                response = await client.get(path, params=params)
            if not response.is_error:
                try:
                    payload = response.json()
                except ValueError as error:
                    raise HTTPException(status_code=502, detail="remote analyst archive returned invalid JSON") from error
                if not isinstance(payload, dict):
                    raise HTTPException(status_code=502, detail="remote analyst archive returned an invalid JSON envelope")
                return payload
            retryable = response.status_code in {429, 500, 502, 503, 504}
            if retryable and attempt < 3:
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
