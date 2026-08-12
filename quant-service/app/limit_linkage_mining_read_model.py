"""Read-only projection of live limit-up linkage research candidates."""

from __future__ import annotations

from typing import Any


def latest_limit_linkage_mining(database: Any, limit: int = 30) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 50))
    with database.transaction() as connection:
        run = connection.execute(
            """SELECT linkage_run_id,observed_at,trade_date,status,summary,created_at
                 FROM quant.intraday_limit_linkage_mining_runs ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
        if run is None:
            return {"run": None, "items": [], "notice": "等待涨停池和下一次五分钟行情快照同时可用。"}
        rows = connection.execute(
            """SELECT rank,symbol,name,score,shared_concepts,concept_labels,leader_symbols,leader_names,
                      pct_change,main_net_inflow,volume_ratio,turnover_rate,evidence,risk_flags
                 FROM quant.intraday_limit_linkage_candidates WHERE linkage_run_id=%s ORDER BY rank LIMIT %s""",
            (run["linkage_run_id"], bounded),
        ).fetchall()
    return {"run": dict(run), "items": [dict(row) for row in rows],
            "notice": "仅以涨停事实、共享精确概念和同刻量价为证据；不按名称推断关联，不构成买卖指令。"}
