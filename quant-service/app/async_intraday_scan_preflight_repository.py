"""Native-async local evidence reads used before an intraday scan persists.

These queries intentionally read only already-recorded local evidence.  They
do not call a provider, alter scan state, or participate in the following
scan-write transaction.
"""

from __future__ import annotations

from typing import Any


async def latest_board_report(async_database: Any) -> dict[str, Any] | None:
    """Return the most recent completed board-flow receipt, if one exists."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT observed_at,status FROM quant.intraday_board_reports
                 WHERE status='completed' ORDER BY observed_at DESC LIMIT 1"""
        )
        row = await result.fetchone()
    return dict(row) if row else None


async def latest_fast_quotes(async_database: Any, symbols: list[str]) -> list[dict[str, Any]]:
    """Return one persisted Super GET cross-check per requested symbol."""
    if not symbols:
        return []
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT DISTINCT ON(symbol) symbol,observed_at,price,pct_change,raw
                 FROM quant.intraday_quote_observations
                WHERE source_name='tushare_super_get_rt_k' AND symbol=ANY(%s)
                ORDER BY symbol,observed_at DESC""",
            (symbols,),
        )
        return [dict(row) for row in await result.fetchall()]


__all__ = ["latest_board_report", "latest_fast_quotes"]
