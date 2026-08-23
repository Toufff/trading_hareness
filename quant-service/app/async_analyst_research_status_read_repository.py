"""Native-async status projection for local analyst research evidence."""

from __future__ import annotations

from datetime import date
from typing import Any


async def status(async_database: Any, as_of_date: date | None = None) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        latest_result = await connection.execute(
            "SELECT as_of_date,status,result,created_at FROM quant.analyst_expert_runs ORDER BY as_of_date DESC LIMIT 1"
        )
        counts_result = await connection.execute(
            "SELECT factor_status,count(*)::int count FROM quant.analyst_opinions GROUP BY factor_status ORDER BY factor_status"
        )
        mappings_result = await connection.execute(
            "SELECT count(*)::int count FROM quant.analyst_theme_board_aliases WHERE status='approved'"
        )
        latest_research_result = await connection.execute(
            "SELECT as_of_date,status,result,created_at FROM quant.analyst_research_runs ORDER BY as_of_date DESC LIMIT 1"
        )
        profiles_result = await connection.execute(
            "SELECT independence_class,count(*)::int count FROM quant.analyst_research_profiles GROUP BY independence_class ORDER BY independence_class"
        )
        latest = await latest_result.fetchone()
        counts = [dict(row) for row in await counts_result.fetchall()]
        mappings = await mappings_result.fetchone()
        latest_research = await latest_research_result.fetchone()
        profiles = [dict(row) for row in await profiles_result.fetchall()]
    return {
        "as_of_date": str(as_of_date) if as_of_date else None,
        "latest_expert_run": dict(latest) if latest else None,
        "latest_research_run": dict(latest_research) if latest_research else None,
        "opinion_status_counts": counts,
        "approved_theme_board_aliases": int(mappings["count"]) if mappings else 0,
        "analyst_provenance_profiles": profiles,
        "boundary": "first local receipt only; research-only; no media fetching; no live strategy weight",
    }


__all__ = ["status"]
