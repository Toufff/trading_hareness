"""Immutable writes for verified broker facts and terminal trade plans."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from psycopg.types.json import Json

from .personal_decision_contracts import BrokerPortfolioSnapshotInput, PersonalTradePlanInput


class ImmutableDecisionFactConflict(ValueError):
    """The caller reused a source key for different immutable content."""


def _content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def persist_broker_snapshot(connection: Any, snapshot: BrokerPortfolioSnapshotInput) -> dict[str, Any]:
    payload = snapshot.model_dump(mode="json")
    content_hash = _content_hash(payload)
    existing = connection.execute(
        """SELECT snapshot_id,content_hash,observed_at,verification
             FROM quant.broker_portfolio_snapshots
            WHERE account_key=%s AND source=%s AND source_snapshot_key=%s""",
        (snapshot.account_key, snapshot.source, snapshot.source_snapshot_key),
    ).fetchone()
    if existing:
        if str(existing["content_hash"]) != content_hash:
            raise ImmutableDecisionFactConflict("source_snapshot_key already exists with different content")
        return {"status": "idempotent", "snapshot_id": existing["snapshot_id"], "content_hash": content_hash}

    for position in snapshot.positions:
        connection.execute(
            """INSERT INTO quant.instruments(symbol,exchange,name,source)
               VALUES(%s,%s,%s,%s)
               ON CONFLICT(symbol) DO UPDATE SET name=COALESCE(NULLIF(EXCLUDED.name,''),quant.instruments.name)""",
            (position.symbol, position.symbol.rsplit(".", 1)[-1], position.name, snapshot.source),
        )
    row = connection.execute(
        """INSERT INTO quant.broker_portfolio_snapshots(
               account_key,source,source_snapshot_key,observed_at,verification,cash,total_asset,
               total_market_value,content_hash,metadata)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING snapshot_id,observed_at,verification""",
        (
            snapshot.account_key, snapshot.source, snapshot.source_snapshot_key, snapshot.observed_at,
            snapshot.verification, snapshot.cash, snapshot.total_asset, snapshot.total_market_value,
            content_hash, Json(snapshot.metadata),
        ),
    ).fetchone()
    for position in snapshot.positions:
        connection.execute(
            """INSERT INTO quant.broker_position_snapshots(
                   snapshot_id,symbol,name,quantity,sellable_quantity,average_cost,market_price,
                   market_value,unrealized_pnl,position_weight_pct,metadata)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                row["snapshot_id"], position.symbol, position.name, position.quantity,
                position.sellable_quantity, position.average_cost, position.market_price,
                position.market_value, position.unrealized_pnl, position.position_weight_pct,
                Json(position.metadata),
            ),
        )
    return {
        "status": "created", "snapshot_id": row["snapshot_id"], "content_hash": content_hash,
        "observed_at": row["observed_at"], "verification": row["verification"],
        "position_count": len(snapshot.positions),
    }


def persist_trade_plan(connection: Any, plan: PersonalTradePlanInput) -> dict[str, Any]:
    payload = plan.model_dump(mode="json")
    content_hash = _content_hash(payload)
    existing = connection.execute(
        "SELECT plan_id,content_hash FROM quant.personal_trade_plans WHERE plan_key=%s", (plan.plan_key,),
    ).fetchone()
    if existing:
        if str(existing["content_hash"]) != content_hash:
            raise ImmutableDecisionFactConflict("plan_key already exists with different content")
        return {"status": "idempotent", "plan_id": existing["plan_id"], "content_hash": content_hash}
    connection.execute(
        """INSERT INTO quant.instruments(symbol,exchange,name,source)
           VALUES(%s,%s,%s,'personal_trade_plan')
           ON CONFLICT(symbol) DO UPDATE SET name=COALESCE(NULLIF(EXCLUDED.name,''),quant.instruments.name)""",
        (plan.symbol, plan.symbol.rsplit(".", 1)[-1], plan.name),
    )
    row = connection.execute(
        """INSERT INTO quant.personal_trade_plans(
               plan_key,plan_kind,symbol,name,as_of_at,valid_until,action,entry_zone,add_trigger,
               reduce_trigger,exit_trigger,stop_price,target_prices,max_position_pct,rationale,
               evidence_refs,risk_flags,metadata,content_hash)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING plan_id,as_of_at,valid_until""",
        (
            plan.plan_key, plan.plan_kind, plan.symbol, plan.name, plan.as_of_at, plan.valid_until,
            plan.action, Json(plan.entry_zone.model_dump(mode="json")) if plan.entry_zone else None,
            plan.add_trigger, plan.reduce_trigger, plan.exit_trigger, plan.stop_price,
            Json([str(value) for value in plan.target_prices]), plan.max_position_pct,
            Json(plan.rationale), Json(plan.evidence_refs), Json(plan.risk_flags), Json(plan.metadata), content_hash,
        ),
    ).fetchone()
    return {
        "status": "created", "plan_id": row["plan_id"], "content_hash": content_hash,
        "as_of_at": row["as_of_at"], "valid_until": row["valid_until"],
    }


__all__ = [
    "ImmutableDecisionFactConflict", "persist_broker_snapshot", "persist_trade_plan",
]
