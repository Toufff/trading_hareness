"""Native-async projections for persisted analyst research evidence.

These views intentionally expose only local evidence ledgers.  They neither
run an analyst sync nor derive a live trading weight.
"""

from __future__ import annotations

from typing import Any


async def profiles(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT a.remote_analyst_id,a.name,p.independence_class,p.audience_size,p.audience_as_of,p.evidence,p.updated_at
                 FROM quant.remote_analysts a LEFT JOIN quant.analyst_research_profiles p USING(remote_analyst_id)
                 ORDER BY a.remote_analyst_id"""
        )
        rows = [dict(row) for row in await result.fetchall()]
    return {
        "items": rows,
        "boundary": "manual provenance only; it is an explicit prior, not inferred from outcomes",
    }


async def observations(async_database: Any, analyst_id: str | None, limit: int) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 500))
    async with async_database.transaction() as connection:
        rows_result = await connection.execute(
            """SELECT observation_id,analyst_id,source_kind,source_id,source_version,content_hash,
                      strategy_available_at,published_at,stated_at,scope,subject_key,subject_label,
                      action,direction,horizon_days,strength,confidence,conditions,evidence_span,
                      extractor_version,status,created_at
                 FROM quant.analyst_observations
                WHERE (%s::text IS NULL OR analyst_id=%s)
                ORDER BY strategy_available_at DESC LIMIT %s""",
            (analyst_id, analyst_id, bounded),
        )
        health_result = await connection.execute(
            """SELECT analyst_id,count(*)::int observations,
                      count(*) FILTER (WHERE status='eligible')::int eligible,
                      count(*) FILTER (WHERE status='replay_only')::int replay_only,
                      max(strategy_available_at) latest_available_at
                 FROM quant.analyst_observations
                WHERE (%s::text IS NULL OR analyst_id=%s)
                GROUP BY analyst_id ORDER BY analyst_id""",
            (analyst_id, analyst_id),
        )
        rows = [dict(row) for row in await rows_result.fetchall()]
        health = [dict(row) for row in await health_result.fetchall()]
    return {
        "items": rows,
        "health": health,
        "live_effect": "none",
        "boundary": "append_only text-derived observations; promotion registry remains zero by default",
    }


__all__ = ["observations", "profiles"]
