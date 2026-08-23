"""Native-async bounded operations at the intraday Feishu outbox edge.

The delivery service retains its multi-table attempt state machine through the
bounded synchronous transaction executor.  This module only handles the two
short per-scan operations: creating a durable pending row before network I/O
and selecting a bounded due retry set.
"""

from __future__ import annotations

import uuid
from typing import Any


async def create_pending(async_database: Any, signal_event_id: uuid.UUID, text: str) -> uuid.UUID:
    """Persist an outbox receipt before an alert can be sent."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """INSERT INTO quant.intraday_alert_deliveries(
                   signal_event_id,channel,status,message_text,next_attempt_at
               ) VALUES(%s,'feishu_adapter','pending',%s,now()) RETURNING delivery_id""",
            (signal_event_id, text),
        )
        row = await result.fetchone()
    return row["delivery_id"]


async def due_deliveries(async_database: Any, max_attempts: int, limit: int) -> list[dict[str, Any]]:
    """Select at most ten retryable, unsent Feishu deliveries."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT d.delivery_id,d.signal_event_id,d.message_text
                 FROM quant.intraday_alert_deliveries d
                WHERE d.channel='feishu_adapter' AND d.status IN ('pending','failed')
                  AND d.message_text IS NOT NULL AND d.message_text<>''
                  AND d.attempt_count<%s
                  AND coalesce(d.next_attempt_at,d.created_at)<=now()
                  AND NOT EXISTS (
                      SELECT 1 FROM quant.intraday_alert_deliveries sent
                       WHERE sent.signal_event_id=d.signal_event_id AND sent.status='sent'
                  )
                ORDER BY d.created_at LIMIT %s""",
            (max_attempts, max(1, min(limit, 10))),
        )
        rows = await result.fetchall()
    return [dict(row) for row in rows]


__all__ = ["create_pending", "due_deliveries"]
