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
    """Return a bounded priority-ordered core basket for post-close supplements."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            f"{CORE_SYMBOLS_SQL} LIMIT %s",
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
