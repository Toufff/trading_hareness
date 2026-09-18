"""Native async read projections for stored trade-discipline plans.

Read-only by construction: every statement here is a ``SELECT`` issued on the
``AsyncDatabase`` pool, so a dashboard refresh never consumes a bounded
blocking-executor slot.  Writes stay in the synchronous
``app/trade_discipline/repository.py``, which the CLI drives.

The column list and the default status filter are imported from that repository
rather than copied, so a stored column can never drift between the writer and
this reader.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .trade_discipline.repository import ACTIVE_STATUSES, PLAN_COLUMNS

EVALUATION_COLUMNS = """evaluation_id,plan_id,as_of_at,trading_date,basis,line_states,plan_state,
                        inputs_hash,content_hash,created_at"""
MAX_LIMIT = 200


def _limit(value: Any, maximum: int = MAX_LIMIT) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(requested, maximum))


def valid_uuid(value: str) -> str | None:
    """``None`` for anything that is not a plan identifier, so a bad path 404s."""
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


async def latest_plans(async_database: Any, account_key: str, *,
                       statuses: tuple[str, ...] = ACTIVE_STATUSES, limit: int = 50) -> list[dict[str, Any]]:
    """The newest plan per symbol for one account, newest first."""
    async with async_database.transaction() as connection:
        result = await connection.execute(f"""
            SELECT * FROM (
              SELECT DISTINCT ON (symbol) {PLAN_COLUMNS}
                FROM quant.discipline_plans
               WHERE account_key=%s AND status = ANY(%s)
               ORDER BY symbol,as_of_at DESC) latest
            ORDER BY as_of_at DESC,symbol LIMIT %s""",
                                          (account_key, list(statuses), _limit(limit)))
        return [dict(row) for row in await result.fetchall()]


async def read_plan(async_database: Any, plan_id: str) -> dict[str, Any] | None:
    """One stored plan, or ``None`` when the identifier is unknown or malformed."""
    resolved = valid_uuid(plan_id)
    if resolved is None:
        return None
    async with async_database.transaction() as connection:
        result = await connection.execute(
            f"SELECT {PLAN_COLUMNS} FROM quant.discipline_plans WHERE plan_id=%s", (resolved,))
        row = await result.fetchone()
    return dict(row) if row else None


async def latest_evaluation(async_database: Any, plan_id: str, *, basis: str | None = None) -> dict[str, Any] | None:
    """The newest evaluation of one plan, optionally restricted to one basis."""
    resolved = valid_uuid(plan_id)
    if resolved is None:
        return None
    async with async_database.transaction() as connection:
        result = await connection.execute(f"""
            SELECT {EVALUATION_COLUMNS} FROM quant.discipline_evaluations
             WHERE plan_id=%s AND (%s::text IS NULL OR basis=%s)
             ORDER BY as_of_at DESC LIMIT 1""", (resolved, basis, basis))
        row = await result.fetchone()
    return dict(row) if row else None


__all__ = ["EVALUATION_COLUMNS", "MAX_LIMIT", "latest_evaluation", "latest_plans", "read_plan", "valid_uuid"]
