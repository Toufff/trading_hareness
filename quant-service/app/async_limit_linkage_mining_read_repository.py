"""Native-async projection of persisted limit-up linkage research evidence."""

from __future__ import annotations

from typing import Any


async def latest_limit_linkage_mining(async_database: Any, limit: int = 30) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 50))
    async with async_database.transaction() as connection:
        run_result = await connection.execute(
            """SELECT linkage_run_id,observed_at,trade_date,status,summary,created_at
                 FROM quant.intraday_limit_linkage_mining_runs ORDER BY observed_at DESC LIMIT 1"""
        )
        run = await run_result.fetchone()
        if run is None:
            return {"run": None, "items": [], "notice": "等待涨停池和下一次五分钟行情快照同时可用。"}
        run = dict(run)
        rows_result = await connection.execute(
            """SELECT rank,symbol,name,score,shared_concepts,concept_labels,leader_symbols,leader_names,
                      pct_change,main_net_inflow,volume_ratio,turnover_rate,evidence,risk_flags
                 FROM quant.intraday_limit_linkage_candidates WHERE linkage_run_id=%s ORDER BY rank LIMIT %s""",
            (run["linkage_run_id"], bounded),
        )
        rows = [dict(row) for row in await rows_result.fetchall()]
    return {"run": run, "items": rows,
            "notice": "仅以涨停事实、共享精确概念和同刻量价为证据；不按名称推断关联，不构成买卖指令。"}


__all__ = ["latest_limit_linkage_mining"]
