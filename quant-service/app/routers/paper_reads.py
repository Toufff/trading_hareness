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
            barriers = connection.execute(
                """SELECT signal_event_id,label_key,upper_return,lower_return,max_horizon_minutes,
                          entry_observed_at,entry_price,exit_observed_at,exit_price,label,raw_return,status,
                          tradability,source_status,calculated_at
                     FROM quant.paper_barrier_outcomes ORDER BY calculated_at DESC LIMIT %s""", (bounded,)
            ).fetchall()
        return {
            "mode": "paper_research_only",
            "live_orders": False,
            "decisions": [dict(row) for row in decisions],
            "positions": [dict(row) for row in positions],
            "latest_portfolio": dict(latest) if latest else None,
            "risk_events": [dict(row) for row in risks],
            "barrier_outcomes": [dict(row) for row in barriers],
            "boundary": "no_automatic_order; historical_replay_not_run",
        }

    @router.get("/api/v1/strategy/funnel")
    def funnel(limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 500))
        with database.transaction() as connection:
            counts = connection.execute(
                """SELECT
                    (SELECT count(*) FROM quant.intraday_quote_observations WHERE observed_at >= now()-interval '1 day')::int AS quote_observations,
                    (SELECT count(*) FROM quant.intraday_signal_events WHERE observed_at >= now()-interval '1 day')::int AS signal_events,
                    (SELECT count(*) FROM quant.intraday_signal_episodes WHERE first_observed_at >= now()-interval '1 day')::int AS episodes,
                    (SELECT count(*) FROM quant.intraday_signal_episodes WHERE first_observed_at >= now()-interval '1 day' AND state='active')::int AS active_episodes,
                    (SELECT count(*) FROM quant.paper_decisions WHERE decision_at >= now()-interval '1 day')::int AS paper_proposals,
                    (SELECT count(*) FROM quant.paper_decisions WHERE decision_at >= now()-interval '1 day' AND status='blocked')::int AS blocked_proposals"""
            ).fetchone()
            timeline = connection.execute(
                """SELECT e.episode_id,e.symbol,e.strategy_key,e.strategy_version,e.direction,e.state,e.stage,
                          e.first_observed_at,e.last_observed_at,e.rearm_count,
                          count(s.signal_event_id)::int AS event_count,
                          max(s.state) FILTER (WHERE s.state='alerted') AS alert_state
                     FROM quant.intraday_signal_episodes e
                     LEFT JOIN quant.intraday_signal_events s ON s.episode_id=e.episode_id
                    WHERE e.first_observed_at >= now()-interval '1 day'
                    GROUP BY e.episode_id ORDER BY e.last_observed_at DESC LIMIT %s""", (bounded,)
            ).fetchall()
        return {"funnel": dict(counts) if counts else {}, "episodes": [dict(row) for row in timeline],
                "boundary": "automatic_candidates_are_frontend_only; paper proposals are not orders"}

    @router.get("/api/v1/strategy/contracts")
    def contracts() -> dict[str, Any]:
        with database.transaction() as connection:
            rows = connection.execute(
                """SELECT strategy_key,strategy_version,status,contract,trial_id,approved_by,approved_at,updated_at
                     FROM quant.strategy_contracts ORDER BY strategy_key,strategy_version"""
            ).fetchall()
        return {"items": [dict(row) for row in rows], "live_effect": "none"}

    @router.get("/api/v1/strategy/governance")
    def governance() -> dict[str, Any]:
        with database.transaction() as connection:
            trials = connection.execute(
                """SELECT trial_id,strategy_key,strategy_version,status,hypothesis,data_boundary,parameters,
                          approved_by,approved_at,created_at,updated_at
                     FROM quant.strategy_trials ORDER BY updated_at DESC"""
            ).fetchall()
            contracts = connection.execute(
                """SELECT strategy_key,strategy_version,status,trial_id,approved_by,approved_at,updated_at
                     FROM quant.strategy_contracts ORDER BY strategy_key,strategy_version"""
            ).fetchall()
        return {"trials": [dict(row) for row in trials], "contracts": [dict(row) for row in contracts],
                "live_effect": "none", "promotion_boundary": "research_only_until_replay_and_human_approval"}

    return router


__all__ = ["build_paper_reads_router"]
