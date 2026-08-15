"""Read-only evidence queries for one-minute board-flow rotations."""

from __future__ import annotations

from typing import Any


def latest_board_rotation_events(database: Any, limit: int = 30) -> dict[str, Any]:
    """Return stored rotation evidence only; never refreshes an upstream board."""
    bounded_limit = max(1, min(int(limit), 100))
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT e.rotation_event_id,e.snapshot_minute,e.event_key,e.taxonomy_key,e.sector_key,e.label,
                      e.event_type,e.direction,e.state,e.first_observed_at,e.last_observed_at,
                      e.confirmation_deadline,e.conditions,e.created_at,e.updated_at,
                      d.status AS delivery_status,d.attempt_count,d.next_attempt_at,d.sent_at,d.error_message
                 FROM quant.intraday_board_rotation_events e
                 LEFT JOIN quant.intraday_board_rotation_deliveries d
                   ON d.rotation_event_id=e.rotation_event_id AND d.channel='feishu_adapter'
                ORDER BY e.last_observed_at DESC,e.created_at DESC LIMIT %s""",
            (bounded_limit,),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "notice": "仅为已保存的东财相邻分钟资金流证据；confirmed 表示已通过下一分钟方向确认。板块挖掘只进入前端研究台，不发送飞书。",
    }
