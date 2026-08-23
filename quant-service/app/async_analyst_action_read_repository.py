"""Native-async analyst action replay and outcome-summary projections."""

from __future__ import annotations

from datetime import date
from typing import Any

from .analyst_trade_action_read_model import (
    anqiang_trade_action_replay_query,
    project_anqiang_trade_action_replay,
)


async def anqiang_trade_action_replay(async_database: Any, as_of_date: date | None, limit: int) -> dict[str, Any]:
    query, params, bounded_limit = anqiang_trade_action_replay_query(as_of_date, limit)
    async with async_database.transaction() as connection:
        result = await connection.execute(query, params)
        rows = [dict(row) for row in await result.fetchall()]
    return project_anqiang_trade_action_replay(rows, as_of_date, bounded_limit)


async def anqiang_trade_action_outcomes(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT methodology_version,horizon_minutes,status,count(*)::int AS count,
                      avg(directional_return) AS avg_directional_return
                 FROM quant.analyst_action_intraday_outcomes
                GROUP BY methodology_version,horizon_minutes,status
                ORDER BY methodology_version,horizon_minutes,status"""
        )
        rows = [dict(row) for row in await result.fetchall()]
    return {
        "analyst_id": "anqiang-touzi-riji", "outcomes": rows,
        "data_boundary": "author-stated-time retrospective replay only; no live strategy effect",
    }


__all__ = ["anqiang_trade_action_outcomes", "anqiang_trade_action_replay"]
