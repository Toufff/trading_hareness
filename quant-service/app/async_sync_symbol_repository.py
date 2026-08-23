"""Native-async bounded fallback universes for daily synchronization.

Configured/requested symbols remain a pure environment/request concern.  This
repository is only used when neither is present, first preferring the explicit
core universe and then the exact stock claims already recorded locally.
"""

from __future__ import annotations

from typing import Any


async def core_symbols(async_database: Any) -> list[str]:
    """Return enabled core symbols in their configured priority order."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT symbol FROM quant.universe_members
                 WHERE universe_key='core' AND enabled
                 ORDER BY priority,symbol""",
        )
        rows = await result.fetchall()
    return [str(row["symbol"]) for row in rows]


async def analyst_claim_symbols(async_database: Any) -> list[str]:
    """Return only syntactically valid stock subjects from local analyst claims."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT DISTINCT subject_key FROM quant.analyst_claims
                 WHERE scope='stock' AND subject_key ~ '^\\d{6}\\.(SH|SZ|BJ)$'""",
        )
        rows = await result.fetchall()
    return [str(row["subject_key"]) for row in rows]


__all__ = ["analyst_claim_symbols", "core_symbols"]
