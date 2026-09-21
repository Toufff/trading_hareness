"""Authoritative scope for intraday discipline-line alerts.

Alerts are not generated for every stored plan.  The holding side is the
intersection of the latest trustworthy broker snapshot and an active plan
derived from that exact snapshot.  The prospective-buy side is the intersection
of the current formal recommendation decision and an active plan whose frozen
evidence names that exact decision.  Anything ambiguous is returned as a
blocker and fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any

from ..broker_snapshot_freshness import broker_freshness
from .repository import PLAN_COLUMNS, plan_from_row


@dataclass(frozen=True)
class AlertScope:
    account_key: str
    plans: tuple[Any, ...] = ()
    snapshot_id: str | None = None
    recommendation_decision_id: str | None = None
    blockers: tuple[str, ...] = ()
    excluded: tuple[dict[str, str], ...] = field(default_factory=tuple)
    expected_holdings: tuple[str, ...] = ()
    expected_recommendations: tuple[str, ...] = ()

    @property
    def coverage(self) -> dict[str, Any]:
        held = {p.symbol for _, p in self.plans if p.plan_kind == 'holding'}
        buys = {p.symbol for _, p in self.plans if p.plan_kind == 'new_buy'}
        def section(expected, actual):
            expected = set(expected)
            return {'expected': len(expected), 'covered': len(expected & actual),
                    'expected_symbols': sorted(expected), 'covered_symbols': sorted(expected & actual),
                    'missing_symbols': sorted(expected - actual)}
        return {'holdings': section(self.expected_holdings, held),
                'recommendations': section(self.expected_recommendations, held | buys)}

    @property
    def has_coverage_gaps(self) -> bool:
        return bool(self.blockers or any(part['missing_symbols'] for part in self.coverage.values()))

    @property
    def fingerprint(self) -> str:
        value = {'snapshot_id': self.snapshot_id, 'decision_id': self.recommendation_decision_id,
                 'plans': sorted(str(pid) for pid, _ in self.plans), 'coverage': self.coverage,
                 'blockers': self.blockers, 'excluded': self.excluded}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _one(connection: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = connection.execute(sql, params).fetchone()
    return dict(row) if row else None


def _rows(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _has_alertable_line(plan: Any) -> bool:
    return any(line.execute_by == "price" and line.confirm.basis in {"minute", "daily"} for line in plan.lines)


def _snapshot_reference(plan: Any) -> str | None:
    return str(plan.position.snapshot_id) if plan.position is not None else None


def _decision_reference(plan: Any) -> str | None:
    prefix = "recommendation_decision:"
    values = [str(item)[len(prefix):] for item in plan.evidence_refs if str(item).startswith(prefix)]
    return values[-1] if values else None


def load_alert_scope(connection: Any, *, account_key: str, as_of: datetime) -> AlertScope:
    """Read and validate the only plans allowed into the alert runtime."""
    blockers: list[str] = []
    excluded: list[dict[str, str]] = []
    accepted: list[Any] = []

    snapshot = _one(connection, """
        SELECT snapshot_id,account_key,observed_at,verification,metadata
          FROM quant.broker_portfolio_snapshots
         WHERE account_key=%s AND verification='verified_exact' AND observed_at<=%s
         ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1""", (account_key, as_of))
    snapshot_id: str | None = None
    held_symbols: set[str] = set()
    if snapshot is None:
        blockers.append("holding_snapshot_missing")
    else:
        freshness = broker_freshness(snapshot, as_of)
        if not freshness["current"]:
            blockers.append("holding_snapshot_stale_or_unverified")
        else:
            snapshot_id = str(snapshot["snapshot_id"])
            held_symbols = {
                str(row["symbol"])
                for row in _rows(connection, """
                    SELECT symbol FROM quant.broker_position_snapshots
                     WHERE snapshot_id=%s AND quantity>0 ORDER BY symbol""", (snapshot["snapshot_id"],))
            }

    decision = _one(connection, """
        SELECT decision_id,result,created_at
          FROM quant.recommendation_pool_decisions
         WHERE created_at<=%s
           AND result->>'status'='ready'
           AND coalesce((result->>'sync_allowed')::boolean,false)
           AND (result->>'valid_until') IS NOT NULL
           AND (result->>'valid_until')::timestamptz>=%s
         ORDER BY as_of_date DESC,created_at DESC,decision_id DESC LIMIT 1""", (as_of, as_of))
    decision_id = str(decision["decision_id"]) if decision else None
    recommended_symbols = {
        str(item.get("symbol"))
        for item in ((decision or {}).get("result") or {}).get("recommended") or []
        if isinstance(item, dict) and item.get("symbol")
    }
    if decision is None:
        blockers.append("formal_recommendation_missing_or_inactive")

    rows = _rows(connection, f"""
        SELECT * FROM (
          SELECT DISTINCT ON (symbol,plan_kind) {PLAN_COLUMNS}
            FROM quant.discipline_plans
           WHERE account_key=%s AND status='active' AND valid_until>=%s
             AND as_of_at<=%s AND created_at<=%s
           ORDER BY symbol,plan_kind,as_of_at DESC,created_at DESC
        ) latest ORDER BY symbol,plan_kind""", (account_key, as_of, as_of, as_of))
    for row in rows:
        plan = plan_from_row(row)
        reason: str | None = None
        if not _has_alertable_line(plan):
            reason = "no_price_basis_line"
        elif plan.plan_kind == "holding":
            if snapshot_id is None or plan.symbol not in held_symbols:
                reason = "not_in_current_trustworthy_holdings"
            elif _snapshot_reference(plan) != snapshot_id:
                reason = "holding_plan_snapshot_mismatch"
        elif plan.plan_kind == "new_buy":
            if decision_id is None or plan.symbol not in recommended_symbols:
                reason = "not_in_current_formal_recommendation"
            elif _decision_reference(plan) != decision_id:
                reason = "recommendation_decision_evidence_mismatch"
        else:
            reason = "unsupported_plan_kind"
        if reason:
            excluded.append({"plan_id": str(row["plan_id"]), "symbol": plan.symbol,
                             "plan_kind": plan.plan_kind, "reason": reason})
        else:
            # The evaluator receives the immutable database identifier, not the
            # generation run id stored inside the pydantic plan.
            accepted.append((str(row["plan_id"]), plan))

    # One symbol cannot issue two competing instructions.  A current holding
    # plan wins over a prospective-buy plan for the same symbol.
    accepted.sort(key=lambda item: (item[1].symbol, 0 if item[1].plan_kind == "holding" else 1))
    deduped: list[Any] = []
    seen: set[str] = set()
    for plan_id, plan in accepted:
        if plan.symbol in seen:
            excluded.append({"plan_id": plan_id, "symbol": plan.symbol, "plan_kind": plan.plan_kind,
                             "reason": "holding_plan_precedes_new_buy_plan"})
            continue
        seen.add(plan.symbol)
        deduped.append((plan_id, plan))
    return AlertScope(account_key=account_key, plans=tuple(deduped), snapshot_id=snapshot_id,
                      recommendation_decision_id=decision_id, blockers=tuple(blockers),
                      excluded=tuple(excluded), expected_holdings=tuple(sorted(held_symbols)),
                      expected_recommendations=tuple(sorted(recommended_symbols)))


__all__ = ["AlertScope", "load_alert_scope"]
