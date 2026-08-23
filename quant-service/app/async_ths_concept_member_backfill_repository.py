"""Native-async local state reads for bounded THS concept-member batches.

The batch service owns the provider calls and durable member writes.  These
queries only establish whether a same-day exact flow universe exists and
report the persisted batch progress afterwards; they never refresh a catalog
or infer membership from names.
"""

from __future__ import annotations

from datetime import date
from typing import Any


async def existing_flow_rows(async_database: Any, trade_date: date) -> dict[str, int]:
    """Return the same-day persisted THS concept-flow row count."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT count(*)::int rows
                 FROM quant.sector_market_observations
                WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s""",
            (trade_date,),
        )
        row = await result.fetchone()
    return {"rows": int(row["rows"] if row else 0)}


async def member_progress(async_database: Any, trade_date: date) -> dict[str, int]:
    """Return durable exact-member progress for the requested trade date."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT count(*) FILTER (WHERE state IN ('completed','empty'))::int done,
                      count(*) FILTER (WHERE state='failed')::int failed
                 FROM quant.sector_member_sync_state
                WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s""",
            (trade_date,),
        )
        row = await result.fetchone()
    return {
        "done": int(row["done"] if row and row["done"] is not None else 0),
        "failed": int(row["failed"] if row and row["failed"] is not None else 0),
    }


__all__ = ["existing_flow_rows", "member_progress"]
