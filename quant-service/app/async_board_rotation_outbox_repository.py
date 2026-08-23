"""Native-async cleanup for legacy board-rotation Feishu outbox rows.

Board rotation is frontend research evidence only.  Legacy pending rows are
durably suppressed rather than retried to any chat channel.
"""

from __future__ import annotations

from typing import Any


async def suppress_legacy_deliveries(async_database: Any) -> int:
    """Mark every unsent legacy rotation row suppressed in one short transaction."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """UPDATE quant.intraday_board_rotation_deliveries
                  SET status='suppressed',error_message='suppressed: Feishu is reserved for watched-stock strategy signals',
                      next_attempt_at=NULL
                WHERE channel='feishu_adapter' AND status IN ('pending','failed')""",
        )
    return int(result.rowcount or 0)


__all__ = ["suppress_legacy_deliveries"]
