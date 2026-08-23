"""Native-async projections for recorded analyst market reviews."""

from __future__ import annotations

from typing import Any

from .analyst_market_review import METHODOLOGY_VERSION


async def list_reviews(async_database: Any, cadence: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Read previously recorded review evidence without rebuilding a review."""
    bounded = max(1, min(int(limit), 100))
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT review_id,cadence,period_start,period_end,status,methodology_version,summary,generated_at,updated_at
                 FROM quant.analyst_market_reviews
                WHERE (%s::text IS NULL OR cadence=%s)
                ORDER BY period_end DESC,generated_at DESC LIMIT %s""",
            (cadence, cadence, bounded),
        )
        rows = [dict(row) for row in await result.fetchall()]
    return {"items": rows, "live_effect": "none", "methodology_version": METHODOLOGY_VERSION}


async def latest_review(async_database: Any, cadence: str) -> dict[str, Any]:
    result = await list_reviews(async_database, cadence, 1)
    item = result["items"][0] if result["items"] else None
    return {"review": item, "live_effect": "none", "methodology_version": METHODOLOGY_VERSION}


__all__ = ["latest_review", "list_reviews"]
