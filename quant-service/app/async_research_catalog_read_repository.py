"""Native async read projections for the research catalog."""

from __future__ import annotations

from typing import Any

from .repo_common import bounded_limit

_limit = bounded_limit

#: A universe listing was previously unbounded; the whole-market ``all_a``
#: universe is a few thousand symbols, so cap defensively and report
#: truncation instead of letting the response grow with the universe size.
_MAX_UNIVERSE_MEMBERS = 10000


async def universe_members(async_database: Any, universe_key: str) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        limit = _MAX_UNIVERSE_MEMBERS
        result = await conn.execute(
            """SELECT m.universe_key,m.symbol,m.enabled,m.priority,m.source,m.metadata,m.added_at,m.updated_at,
                      i.name,i.industry,i.is_st FROM quant.universe_members m JOIN quant.instruments i ON i.symbol=m.symbol
               WHERE m.universe_key=%s ORDER BY m.priority,m.symbol LIMIT %s""", (universe_key, limit + 1))
        rows = await result.fetchall()
    truncated = len(rows) > limit
    return {"universe_key": universe_key, "items": rows[:limit], "truncated": truncated}


async def latest_features(async_database: Any, universe_key: str, limit: int) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute(
            """SELECT f.snapshot_key,f.as_of_date,f.feature_version,max(f.created_at) created_at
               FROM quant.feature_snapshots f JOIN quant.universe_members m ON m.symbol=f.symbol
               WHERE m.universe_key=%s GROUP BY f.snapshot_key,f.as_of_date,f.feature_version
               ORDER BY created_at DESC LIMIT 1""", (universe_key,))
        snapshot = await result.fetchone()
        if not snapshot:
            return {"snapshot": None, "items": []}
        result = await conn.execute(
            """SELECT f.symbol,i.name,f.features,f.quality_flags FROM quant.feature_snapshots f JOIN quant.instruments i ON i.symbol=f.symbol
               WHERE f.snapshot_key=%s AND f.feature_version=%s ORDER BY f.symbol LIMIT %s""",
            (snapshot["snapshot_key"], snapshot["feature_version"], _limit(limit, 1000)))
        rows = await result.fetchall()
    return {"snapshot": snapshot, "items": rows}


async def factor_registry(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute("SELECT factor_key,label,category,implementation,inputs,formula,framework_tags,version,status,metadata,updated_at FROM quant.factor_registry ORDER BY category,factor_key")
        rows = await result.fetchall()
    return {"items": rows}


async def factor_evaluations(async_database: Any, universe_key: str, limit: int) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute(
            """SELECT e.evaluation_id,e.factor_key,f.label,e.universe_key,e.start_date,e.end_date,e.horizon_days,e.engine,e.status,
                      e.observations,e.cross_section_days,e.metrics,e.artifact,e.created_at
               FROM quant.factor_evaluations e JOIN quant.factor_registry f ON f.factor_key=e.factor_key
               WHERE e.universe_key=%s ORDER BY e.created_at DESC,e.factor_key LIMIT %s""",
            (universe_key, _limit(limit, 500)))
        rows = await result.fetchall()
    return {"items": rows}


async def strategy_registry(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute("SELECT strategy_key,label,engine,version,configuration,status,updated_at FROM quant.strategy_registry ORDER BY strategy_key")
        rows = await result.fetchall()
    return {"items": rows}


async def strategy_experiments(async_database: Any, universe_key: str, limit: int) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute(
            """SELECT e.strategy_experiment_id,e.strategy_key,s.label,e.universe_key,e.start_date,e.end_date,e.status,e.parameters,
                      e.metrics,e.equity_curve,e.trades,e.created_at
               FROM quant.strategy_experiments e JOIN quant.strategy_registry s ON s.strategy_key=e.strategy_key
               WHERE e.universe_key=%s ORDER BY e.created_at DESC LIMIT %s""",
            (universe_key, _limit(limit, 200)))
        rows = await result.fetchall()
    return {"items": rows}


async def data_quality_issues(async_database: Any, limit: int) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute("SELECT * FROM quant.data_quality_issues WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT %s", (_limit(limit, 500),))
        rows = await result.fetchall()
    return {"items": rows}
