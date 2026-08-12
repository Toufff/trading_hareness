"""Small, side-effect-free helpers for bounded provider retries."""

from __future__ import annotations

from typing import Any, Mapping


def retry_delay_seconds(headers: Mapping[str, Any] | None, fallback_seconds: float,
                        *, maximum_seconds: float = 10.0) -> float:
    """Return a bounded retry delay, respecting a valid ``Retry-After`` hint.

    A provider may ask callers to slow down after HTTP 429.  The service keeps
    its existing single-retry contract and caps the pause so a slow control
    plane request cannot occupy a realtime worker indefinitely.  Invalid,
    negative, date-form or unexpectedly long values deliberately fall back to
    the caller's short exponential backoff; the helper never parses arbitrary
    response content and does not perform I/O.
    """
    fallback = max(0.0, float(fallback_seconds))
    maximum = max(fallback, float(maximum_seconds))
    raw = None
    if headers:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        hinted = float(str(raw).strip()) if raw not in (None, "") else None
    except (TypeError, ValueError):
        hinted = None
    if hinted is None or hinted < 0:
        return fallback
    return min(maximum, max(fallback, hinted))


__all__ = ["retry_delay_seconds"]
