"""Native-async bounded local inputs for a live watchlist scan.

The scan service owns all provider calls and its single write transaction.  The
two reads here occur before that transaction and are deliberately limited to
the explicit watch basket plus exact point-in-time memberships.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


async def watchlists(
    async_database: Any,
    requested_symbols: list[str],
    *,
    max_symbols: int,
) -> list[dict[str, Any]]:
    """Load requested watches or one extra row to detect capacity overflow."""
    async with async_database.transaction() as connection:
        if requested_symbols:
            result = await connection.execute(
                "SELECT * FROM quant.intraday_watchlists WHERE enabled AND symbol=ANY(%s) ORDER BY symbol",
                (requested_symbols,),
            )
        else:
            result = await connection.execute(
                "SELECT * FROM quant.intraday_watchlists WHERE enabled "
                "ORDER BY available_quantity DESC,updated_at DESC,symbol LIMIT %s",
                (max(1, int(max_symbols)) + 1,),
            )
        rows = await result.fetchall()
    return [dict(row) for row in rows]


async def enabled_watches(async_database: Any, *, max_symbols: int) -> list[dict[str, Any]]:
    """Load the bounded enabled basket for a manual minute-profile capture.

    Unlike :func:`watchlists`, this path does not need an overflow sentinel:
    the capture itself is deliberately bounded to ``max_symbols``.
    """
    async with async_database.transaction() as connection:
        result = await connection.execute(
            "SELECT * FROM quant.intraday_watchlists WHERE enabled "
            "ORDER BY available_quantity DESC,updated_at DESC,symbol LIMIT %s",
            (max(1, int(max_symbols)),),
        )
        rows = await result.fetchall()
    return [dict(row) for row in rows]


async def exact_memberships(
    async_database: Any,
    symbols: list[str],
    observed_at: datetime,
) -> list[dict[str, Any]]:
    """Read only exact persisted taxonomy/sector links for the watch basket."""
    if not symbols:
        return []
    local_trade_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT taxonomy_key,sector_key,symbol
                 FROM quant.sector_membership_history
                WHERE symbol=ANY(%s) AND effective_from<=%s
                  AND (effective_to IS NULL OR effective_to>=%s)
                  AND taxonomy_key IN ('ths_concept_flow','ths_index_n','ths_industry')""",
            (symbols, local_trade_date, local_trade_date),
        )
        rows = await result.fetchall()
    return [dict(row) for row in rows]


__all__ = ["enabled_watches", "exact_memberships", "watchlists"]
