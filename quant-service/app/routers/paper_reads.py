"""Read-only paper research ledger projections."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter


def build_paper_reads_router(database: Any) -> APIRouter:
    router = APIRouter(tags=["paper-research"])

    @router.get("/api/v1/paper/status")
    def status(limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 200))
        with database.transaction() as connection:
            decisions = connection.execute(
                """SELECT d.decision_id,d.signal_event_id,d.strategy_key,d.strategy_version,d.symbol,
                          d.direction,d.status,d.decision_at,d.target_quantity,d.target_weight,d.evidence,d.risk_flags
                     FROM quant.paper_decisions d ORDER BY d.decision_at DESC LIMIT %s""", (bounded,)
            ).fetchall()
            positions = connection.execute(
                """SELECT symbol,quantity,sellable_quantity,average_cost,buy_date,realized_pnl,updated_at
                     FROM quant.paper_positions ORDER BY symbol"""
            ).fetchall()
            latest = connection.execute(
                """SELECT snapshot_id,as_of,cash,equity,gross_exposure,net_exposure,drawdown,payload
                     FROM quant.paper_portfolio_snapshots ORDER BY as_of DESC LIMIT 1"""
            ).fetchone()
            risks = connection.execute(
                """SELECT risk_event_id,decision_id,symbol,event_type,severity,message,occurred_at,details
                     FROM quant.paper_risk_events ORDER BY occurred_at DESC LIMIT %s""", (bounded,)
            ).fetchall()
        return {
            "mode": "paper_research_only",
            "live_orders": False,
            "decisions": [dict(row) for row in decisions],
            "positions": [dict(row) for row in positions],
            "latest_portfolio": dict(latest) if latest else None,
            "risk_events": [dict(row) for row in risks],
            "boundary": "no_automatic_order; historical_replay_not_run",
        }

    @router.get("/api/v1/strategy/contracts")
    def contracts() -> dict[str, Any]:
        with database.transaction() as connection:
            rows = connection.execute(
                """SELECT strategy_key,strategy_version,status,contract,trial_id,approved_by,approved_at,updated_at
                     FROM quant.strategy_contracts ORDER BY strategy_key,strategy_version"""
            ).fetchall()
        return {"items": [dict(row) for row in rows], "live_effect": "none"}

    return router


__all__ = ["build_paper_reads_router"]
