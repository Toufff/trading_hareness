"""Native-async projections for persisted board rotation and mining evidence."""

from __future__ import annotations

from typing import Any


async def latest_board_rotation_events(async_database: Any, limit: int = 30) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit), 100))
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT e.rotation_event_id,e.snapshot_minute,e.event_key,e.taxonomy_key,e.sector_key,e.label,
                      e.event_type,e.direction,e.state,e.first_observed_at,e.last_observed_at,
                      e.confirmation_deadline,e.conditions,e.created_at,e.updated_at,
                      d.status AS delivery_status,d.attempt_count,d.next_attempt_at,d.sent_at,d.error_message
                 FROM quant.intraday_board_rotation_events e
                 LEFT JOIN quant.intraday_board_rotation_deliveries d
                   ON d.rotation_event_id=e.rotation_event_id AND d.channel='feishu_adapter'
                ORDER BY e.last_observed_at DESC,e.created_at DESC LIMIT %s""",
            (bounded_limit,),
        )
        rows = [dict(row) for row in await result.fetchall()]
    return {
        "items": rows,
        "notice": "仅为已保存的东财相邻分钟资金流证据；confirmed 表示已通过下一分钟方向确认。板块挖掘只进入前端研究台，不发送飞书。",
    }


async def latest_board_stock_mining(async_database: Any, limit: int = 20) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 50))
    async with async_database.transaction() as connection:
        run_result = await connection.execute(
            """SELECT mining_run_id,board_report_id,observed_at,status,coverage,summary,created_at
                 FROM quant.intraday_board_stock_mining_runs
                ORDER BY observed_at DESC LIMIT 1"""
        )
        run = await run_result.fetchone()
        if run is None:
            return {"run": None, "inflow": [], "outflow": [],
                    "notice": "等待下一次五分钟板块报告；仅精确成员映射完整的板块会生成候选。"}
        run = dict(run)
        rows_result = await connection.execute(
            """SELECT rank,direction,setup_key,symbol,name,taxonomy_key,sector_key,label,score,
                      board_net_inflow,board_change_pct,main_net_inflow,volume_ratio,turnover_rate,pct_change,
                      evidence,risk_flags
                 FROM quant.intraday_board_stock_mining_candidates
                WHERE mining_run_id=%s
                ORDER BY direction,rank""",
            (run["mining_run_id"],),
        )
        items = [dict(row) for row in await rows_result.fetchall()]
    return {
        "run": run,
        "inflow": [row for row in items if row["direction"] == "inflow"][:bounded],
        "outflow": [row for row in items if row["direction"] == "outflow"][:bounded],
        "notice": "候选使用东财板块资金流、精确成分股映射及同刻腾讯量价/主力流；不按名称推断成员，也不构成买卖指令。",
    }


__all__ = ["latest_board_rotation_events", "latest_board_stock_mining"]
