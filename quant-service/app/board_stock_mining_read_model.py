"""Read-only projection of the latest board-flow stock-mining run."""

from __future__ import annotations

from typing import Any


def latest_board_stock_mining(database: Any, limit: int = 20) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 50))
    with database.transaction() as connection:
        run = connection.execute(
            """SELECT mining_run_id,board_report_id,observed_at,status,coverage,summary,created_at
                 FROM quant.intraday_board_stock_mining_runs
                ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
        if run is None:
            return {"run": None, "inflow": [], "outflow": [],
                    "notice": "等待下一次五分钟板块报告；仅精确成员映射完整的板块会生成候选。"}
        rows = connection.execute(
            """SELECT rank,direction,setup_key,symbol,name,taxonomy_key,sector_key,label,score,
                      board_net_inflow,board_change_pct,main_net_inflow,volume_ratio,turnover_rate,pct_change,
                      evidence,risk_flags
                 FROM quant.intraday_board_stock_mining_candidates
                WHERE mining_run_id=%s
                ORDER BY direction,rank""",
            (run["mining_run_id"],),
        ).fetchall()
    items = [dict(row) for row in rows]
    return {
        "run": dict(run),
        "inflow": [row for row in items if row["direction"] == "inflow"][:bounded],
        "outflow": [row for row in items if row["direction"] == "outflow"][:bounded],
        "notice": "候选使用东财板块资金流、精确成分股映射及同刻腾讯量价/主力流；不按名称推断成员，也不构成买卖指令。",
    }
