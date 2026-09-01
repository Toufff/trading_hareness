"""Cadence-only capture of the complete Fuyao A-share Level-1 cross-section.

The collector stores provider rows as raw evidence.  Ranking, width and
strategy eligibility remain downstream research projections; this loop never
creates a trading decision.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


async def capture_level1_snapshot(
    *,
    fetch_snapshot: Callable[[], Awaitable[tuple[list[dict[str, Any]], Mapping[str, Any]]]],
    persist: Callable[[str, str, list[dict[str, Any]]], Awaitable[int]],
    session_open: Callable[[datetime], Awaitable[bool]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Capture one all-A snapshot, returning a secret-free health result."""
    observed_at = now or datetime.now(timezone.utc)
    if not await session_open(observed_at):
        return {"status": "outside_session", "received": 0, "stored": 0}
    rows, metadata = await fetch_snapshot()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or row.get("ts_code") or "").upper()
        if not symbol:
            continue
        payloads.append({
            **dict(row),
            "ts_code": symbol,
            "snapshot_observed_at": observed_at.isoformat(),
            "snapshot_metadata": dict(metadata),
            "research_only": True,
        })
    stored = await persist("fuyao_ths", "a_share_prices_snapshot", payloads) if payloads else 0
    return {
        "status": "completed" if payloads else "empty",
        "received": len(payloads),
        "stored": stored,
        "provider": "fuyao_ths",
        "capability": "a_share_prices_snapshot",
        "upstream_timestamp_ms": metadata.get("upstream_timestamp_ms"),
        "cross_sectional": bool(metadata.get("cross_sectional", False)),
    }


async def run_level1_snapshot_loop(
    *,
    interval_seconds: int,
    capture: Callable[[], Awaitable[dict[str, Any]]],
    log: Callable[[str], None] = print,
) -> None:
    """Run at a bounded ~60 second cadence; errors never stop future rounds."""
    while True:
        try:
            result = await capture()
            if result.get("status") not in {"completed", "empty", "outside_session"}:
                log(f"all-A Level-1 capture degraded: {str(result)[:400]}")
        except Exception as error:  # noqa: BLE001 - next cadence is the retry
            log(f"all-A Level-1 capture failed: {str(error)[:300]}")
        await asyncio.sleep(max(10, min(300, int(interval_seconds))))


__all__ = ["capture_level1_snapshot", "run_level1_snapshot_loop"]
