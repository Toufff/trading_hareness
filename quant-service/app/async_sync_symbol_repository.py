"""Native-async bounded fallback universes for daily synchronization.

Configured/requested symbols remain a pure environment/request concern.  This
repository is only used when neither is present, first preferring the explicit
core universe and then the exact stock claims already recorded locally.
"""

from __future__ import annotations

from typing import Any

from .sync_symbol_repository import ANALYST_CLAIM_SYMBOLS_SQL, CORE_SYMBOLS_SQL


async def core_symbols(async_database: Any) -> list[str]:
    """Return enabled core symbols in their configured priority order."""
    async with async_database.transaction() as connection:
        result = await connection.execute(CORE_SYMBOLS_SQL)
        rows = await result.fetchall()
    return [str(row["symbol"]) for row in rows]


async def limited_core_symbols(async_database: Any, limit: int) -> list[str]:
    """Return holdings first, then the bounded core basket for supplements.

    Announcements and event research must never omit a held stock merely
    because a separate watchlist projection has not placed it in ``core``.
    The latest exact broker snapshot is used as an inclusion source only; its
    staleness is still handled independently by the decision brief.
    """
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """WITH latest_snapshot AS (
                   SELECT snapshot_id FROM quant.broker_portfolio_snapshots
                    WHERE verification='verified_exact'
                    ORDER BY observed_at DESC,recorded_at DESC LIMIT 1
               ), candidates AS (
                   SELECT position.symbol,0::integer AS priority
                     FROM quant.broker_position_snapshots position
                     JOIN latest_snapshot snapshot ON snapshot.snapshot_id=position.snapshot_id
                    WHERE position.quantity>0
                   UNION ALL
                   SELECT symbol,1000+priority FROM quant.universe_members
                    WHERE universe_key='core' AND enabled
               )
               SELECT symbol,min(priority)::integer AS priority FROM candidates
                GROUP BY symbol ORDER BY priority,symbol LIMIT %s""",
            (max(1, int(limit)),),
        )
        rows = await result.fetchall()
    return [str(row["symbol"]) for row in rows]


async def analyst_claim_symbols(async_database: Any) -> list[str]:
    """Return only syntactically valid stock subjects from local analyst claims."""
    async with async_database.transaction() as connection:
        result = await connection.execute(ANALYST_CLAIM_SYMBOLS_SQL)
        rows = await result.fetchall()
    return [str(row["subject_key"]) for row in rows]


__all__ = ["analyst_claim_symbols", "core_symbols", "limited_core_symbols"]
