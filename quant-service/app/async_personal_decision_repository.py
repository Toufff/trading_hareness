"""Native-async read model for actual holdings and personal decision briefs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .personal_decision_contracts import assemble_personal_decision_brief


async def _one(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    result = await connection.execute(sql, params)
    return await result.fetchone()


async def _all(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    result = await connection.execute(sql, params)
    return await result.fetchall()


async def latest_broker_snapshot(async_database: Any, account_key: str) -> dict[str, Any] | None:
    async with async_database.transaction() as connection:
        snapshot = await _one(
            connection,
            """SELECT snapshot_id,account_key,source,source_snapshot_key,observed_at,verification,
                      cash,total_asset,total_market_value,content_hash,metadata,recorded_at
                 FROM quant.broker_portfolio_snapshots WHERE account_key=%s
                ORDER BY observed_at DESC,recorded_at DESC LIMIT 1""",
            (account_key,),
        )
        if not snapshot:
            return None
        positions = await _all(
            connection,
            """SELECT symbol,name,quantity,sellable_quantity,average_cost,market_price,market_value,
                      unrealized_pnl,position_weight_pct,metadata
                 FROM quant.broker_position_snapshots WHERE snapshot_id=%s ORDER BY market_value DESC NULLS LAST,symbol""",
            (snapshot["snapshot_id"],),
        )
    return {**dict(snapshot), "positions": [dict(row) for row in positions], "live_orders": False}


async def active_trade_plans(async_database: Any, as_of_at: datetime) -> list[dict[str, Any]]:
    async with async_database.transaction() as connection:
        rows = await _all(
            connection,
            """SELECT DISTINCT ON (plan_kind,symbol)
                      plan_key,plan_kind,symbol,name,as_of_at,valid_until,action,entry_zone,add_trigger,
                      reduce_trigger,exit_trigger,stop_price,target_prices,max_position_pct,rationale,
                      evidence_refs,risk_flags,metadata
                 FROM quant.personal_trade_plans
                WHERE as_of_at<=%s AND valid_until>=%s
                ORDER BY plan_kind,symbol,as_of_at DESC,created_at DESC""",
            (as_of_at, as_of_at),
        )
    return [dict(row) for row in rows]


async def latest_market_section(
    async_database: Any,
    *,
    as_of_at: datetime | None = None,
    max_age: timedelta = timedelta(days=4),
) -> dict[str, Any] | None:
    async with async_database.transaction() as connection:
        row = await _one(
            connection,
            """SELECT review_id,exchange_date,session,observed_at,market_state,data_boundary,report
                 FROM quant.strategy_review_runs ORDER BY observed_at DESC LIMIT 1""",
        )
    if not row:
        return None
    payload = dict(row)
    observed_at = payload.get("observed_at")
    boundary = as_of_at or datetime.now(timezone.utc)
    if isinstance(observed_at, str):
        observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    age = (
        boundary.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
        if isinstance(observed_at, datetime) and observed_at.tzinfo is not None
        else None
    )
    status = "ready" if age is not None and -timedelta(minutes=5) <= age <= max_age else "unavailable"
    return {"status": status, **payload}


async def latest_personal_decision_brief(
    async_database: Any, account_key: str, *, as_of_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at = as_of_at or datetime.now(timezone.utc)
    portfolio = await latest_broker_snapshot(async_database, account_key)
    plans = await active_trade_plans(async_database, observed_at)
    market = await latest_market_section(async_database, as_of_at=observed_at)
    return assemble_personal_decision_brief(
        as_of_at=observed_at, market_section=market, portfolio=portfolio, plans=plans,
    )


__all__ = [
    "active_trade_plans", "latest_broker_snapshot", "latest_market_section", "latest_personal_decision_brief",
]
