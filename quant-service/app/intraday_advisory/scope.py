"""Independent advisory scope: current holdings plus formal recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..broker_snapshot_freshness import broker_freshness


@dataclass(frozen=True)
class ScopeItem:
    symbol: str
    name: str
    source: str
    facts: dict[str, Any]


@dataclass(frozen=True)
class AdvisoryScope:
    account_key: str
    items: tuple[ScopeItem, ...]
    snapshot_id: str | None
    decision_id: str | None
    blockers: tuple[str, ...]
    snapshot_observed_at: datetime | None = None


def _one(connection: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = connection.execute(sql, params).fetchone()
    return dict(row) if row else None


def load_scope(connection: Any, *, account_key: str, as_of: datetime) -> AdvisoryScope:
    blockers: list[str] = []
    by_symbol: dict[str, ScopeItem] = {}
    snapshot = _one(connection, """
        SELECT snapshot_id,account_key,observed_at,verification,metadata
          FROM quant.broker_portfolio_snapshots
         WHERE account_key=%s AND verification='verified_exact' AND observed_at<=%s
         ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1""", (account_key, as_of))
    snapshot_id: str | None = None
    snapshot_observed_at: datetime | None = None
    if snapshot is None:
        blockers.append("holding_snapshot_missing")
    elif not broker_freshness(snapshot, as_of)["current"]:
        blockers.append("holding_snapshot_stale_or_unverified")
    else:
        snapshot_id = str(snapshot["snapshot_id"])
        snapshot_observed_at = snapshot["observed_at"]
        rows = connection.execute("""
            SELECT symbol,name,quantity,sellable_quantity,average_cost,market_price,
                   market_value,unrealized_pnl,position_weight_pct
              FROM quant.broker_position_snapshots
             WHERE snapshot_id=%s AND quantity>0 ORDER BY market_value DESC NULLS LAST,symbol""",
                                  (snapshot["snapshot_id"],)).fetchall()
        for raw in rows:
            row = dict(raw)
            by_symbol[str(row["symbol"])] = ScopeItem(str(row["symbol"]), str(row["name"]), "holding", row)
        if by_symbol:
            focused = connection.execute("""
                SELECT symbol,intent,expires_at FROM quant.intraday_holding_focus
                 WHERE account_key=%s AND expires_at>%s AND symbol=ANY(%s)""",
                (account_key, as_of, list(by_symbol))).fetchall()
            for raw in focused:
                row = dict(raw)
                item = by_symbol[row['symbol']]
                by_symbol[item.symbol] = ScopeItem(item.symbol, item.name, item.source,
                    {**item.facts, 'monitoring_focus': {'intent': row['intent'],
                       'expires_at': row['expires_at']}})

    decision = _one(connection, """
        SELECT decision_id,result,created_at
          FROM quant.recommendation_pool_decisions
         WHERE created_at<=%s AND result->>'status'='ready'
           AND coalesce((result->>'sync_allowed')::boolean,false)
           AND (result->>'valid_until') IS NOT NULL
           AND (result->>'valid_until')::timestamptz>=%s
         ORDER BY as_of_date DESC,created_at DESC,decision_id DESC LIMIT 1""", (as_of, as_of))
    decision_id = str(decision["decision_id"]) if decision else None
    if decision is None:
        blockers.append("formal_recommendation_missing_or_inactive")
    else:
        for item in (decision.get("result") or {}).get("recommended") or []:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            symbol = str(item["symbol"])
            if symbol in by_symbol:  # A real position wins over a prospective idea.
                continue
            by_symbol[symbol] = ScopeItem(symbol, str(item.get("name") or symbol), "recommendation", dict(item))
    return AdvisoryScope(account_key, tuple(by_symbol.values()), snapshot_id, decision_id,
                         tuple(blockers), snapshot_observed_at)


__all__ = ["AdvisoryScope", "ScopeItem", "load_scope"]
