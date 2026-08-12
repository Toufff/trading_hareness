"""Bounded, read-only projections for research catalog and experiment results."""

from __future__ import annotations

from typing import Any


def universe_members(database: Any, universe_key: str) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT m.universe_key,m.symbol,m.enabled,m.priority,m.source,m.metadata,m.added_at,m.updated_at,
                      i.name,i.industry,i.is_st FROM quant.universe_members m JOIN quant.instruments i ON i.symbol=m.symbol
               WHERE m.universe_key=%s ORDER BY m.priority,m.symbol""",
            (universe_key,),
        ).fetchall()
    return {"universe_key": universe_key, "items": rows}


def latest_features(database: Any, universe_key: str, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        snapshot = connection.execute(
            """SELECT f.snapshot_key,f.as_of_date,f.feature_version,max(f.created_at) created_at
               FROM quant.feature_snapshots f JOIN quant.universe_members m ON m.symbol=f.symbol
               WHERE m.universe_key=%s GROUP BY f.snapshot_key,f.as_of_date,f.feature_version
               ORDER BY created_at DESC LIMIT 1""",
            (universe_key,),
        ).fetchone()
        if not snapshot:
            return {"snapshot": None, "items": []}
        rows = connection.execute(
            """SELECT f.symbol,i.name,f.features,f.quality_flags FROM quant.feature_snapshots f JOIN quant.instruments i ON i.symbol=f.symbol
               WHERE f.snapshot_key=%s AND f.feature_version=%s ORDER BY f.symbol LIMIT %s""",
            (snapshot["snapshot_key"], snapshot["feature_version"], max(1, min(limit, 1000))),
        ).fetchall()
    return {"snapshot": snapshot, "items": rows}


def factor_registry(database: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT factor_key,label,category,implementation,inputs,formula,framework_tags,version,status,metadata,updated_at FROM quant.factor_registry ORDER BY category,factor_key"
        ).fetchall()
    return {"items": rows}


def factor_evaluations(database: Any, universe_key: str, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT e.evaluation_id,e.factor_key,f.label,e.universe_key,e.start_date,e.end_date,e.horizon_days,e.engine,e.status,
                      e.observations,e.cross_section_days,e.metrics,e.artifact,e.created_at
               FROM quant.factor_evaluations e JOIN quant.factor_registry f ON f.factor_key=e.factor_key
               WHERE e.universe_key=%s ORDER BY e.created_at DESC,e.factor_key LIMIT %s""",
            (universe_key, max(1, min(limit, 500))),
        ).fetchall()
    return {"items": rows}


def strategy_registry(database: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT strategy_key,label,engine,version,configuration,status,updated_at FROM quant.strategy_registry ORDER BY strategy_key"
        ).fetchall()
    return {"items": rows}


def strategy_experiments(database: Any, universe_key: str, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT e.strategy_experiment_id,e.strategy_key,s.label,e.universe_key,e.start_date,e.end_date,e.status,e.parameters,
                      e.metrics,e.equity_curve,e.trades,e.created_at
               FROM quant.strategy_experiments e JOIN quant.strategy_registry s ON s.strategy_key=e.strategy_key
               WHERE e.universe_key=%s ORDER BY e.created_at DESC LIMIT %s""",
            (universe_key, max(1, min(limit, 200))),
        ).fetchall()
    return {"items": rows}


def data_quality_issues(database: Any, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM quant.data_quality_issues WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT %s",
            (max(1, min(limit, 500)),),
        ).fetchall()
    return {"items": rows}


__all__ = [
    "data_quality_issues", "factor_evaluations", "factor_registry", "latest_features",
    "strategy_experiments", "strategy_registry", "universe_members",
]
