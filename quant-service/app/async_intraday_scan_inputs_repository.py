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


#: Trailing sessions averaged for the volume-ratio denominator.  Five is the
#: conventional 量比 window and matches what the replaced Eastmoney field uses.
VOLUME_RATIO_REFERENCE_SESSIONS = 5


async def watch_flow_reference(
    async_database: Any,
    symbols: list[str],
    observed_at: datetime,
) -> dict[str, dict[str, Any]]:
    """Load the local reference that turns snapshot volume into flow metrics.

    Both columns carry Chinese market units that only this boundary knows:
    ``daily_fundamentals.float_share`` is 万股 and ``canonical_bars_daily.volume``
    is 手, so both are converted to plain shares here and every consumer above
    works in one unit.

    Only sessions strictly before the local trading date are read.  Today's
    end-of-day rows land in the same tables after the close, and an intraday
    derivation that silently started using them would be looking at its own
    answer.
    """
    if not symbols:
        return {}
    local_trade_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """WITH recent AS (
                 SELECT symbol, volume,
                        row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS session_rank
                   FROM quant.canonical_bars_daily
                  WHERE symbol=ANY(%s) AND trading_date<%s AND volume IS NOT NULL AND volume>0
                    AND NOT coalesce(is_suspended, false)
               ), trailing_volume AS (
                 SELECT symbol, avg(volume)*100 AS mean_daily_volume_shares,
                        count(*) AS sample_sessions
                   FROM recent WHERE session_rank<=%s GROUP BY symbol
               ), latest_float AS (
                 SELECT DISTINCT ON (symbol) symbol, float_share*10000 AS float_shares,
                        trading_date AS float_share_date
                   FROM quant.daily_fundamentals
                  WHERE symbol=ANY(%s) AND trading_date<%s AND float_share IS NOT NULL AND float_share>0
                  ORDER BY symbol, trading_date DESC
               )
               SELECT coalesce(trailing_volume.symbol, latest_float.symbol) AS symbol,
                      latest_float.float_shares, latest_float.float_share_date,
                      trailing_volume.mean_daily_volume_shares, trailing_volume.sample_sessions
                 FROM trailing_volume FULL JOIN latest_float ON trailing_volume.symbol=latest_float.symbol""",
            (symbols, local_trade_date, VOLUME_RATIO_REFERENCE_SESSIONS, symbols, local_trade_date),
        )
        rows = await result.fetchall()
    return {
        str(row["symbol"]): {
            "float_shares": float(row["float_shares"]) if row["float_shares"] is not None else None,
            "float_share_date": row["float_share_date"].isoformat() if row["float_share_date"] else None,
            "mean_daily_volume_shares": (
                float(row["mean_daily_volume_shares"]) if row["mean_daily_volume_shares"] is not None else None
            ),
            "sample_sessions": int(row["sample_sessions"] or 0),
        }
        for row in rows if row["symbol"]
    }


__all__ = [
    "VOLUME_RATIO_REFERENCE_SESSIONS", "enabled_watches", "exact_memberships",
    "watch_flow_reference", "watchlists",
]
