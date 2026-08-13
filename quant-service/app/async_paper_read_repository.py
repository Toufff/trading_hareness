"""Native async read projections for the paper-research console."""

from __future__ import annotations

from typing import Any


async def _fetchall(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    result = await connection.execute(sql, params)
    return await result.fetchall()


async def _fetchone(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    result = await connection.execute(sql, params)
    return await result.fetchone()


async def paper_status(async_database: Any, limit: int = 50) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 200))
    async with async_database.transaction() as connection:
        decisions = await _fetchall(connection, """SELECT d.decision_id,d.signal_event_id,d.strategy_key,d.strategy_version,d.symbol,
                          d.direction,d.status,d.decision_at,d.target_quantity,d.target_weight,d.evidence,d.risk_flags
                     FROM quant.paper_decisions d ORDER BY d.decision_at DESC LIMIT %s""", (bounded,))
        positions = await _fetchall(connection, """SELECT symbol,quantity,sellable_quantity,average_cost,buy_date,realized_pnl,updated_at
                     FROM quant.paper_positions ORDER BY symbol""")
        orders = await _fetchall(connection, """SELECT o.order_id,o.decision_id,o.symbol,o.side,o.requested_quantity,o.accepted_quantity,o.filled_quantity,
                          o.average_fill_price,o.status,o.fees,o.slippage,o.submitted_at,o.filled_at,o.metadata
                     FROM quant.paper_orders o ORDER BY o.submitted_at DESC LIMIT %s""", (bounded,))
        accounts = await _fetchall(connection, """SELECT account_key,initial_cash,cash,configured_by,configured_at,updated_at,metadata
                     FROM quant.paper_accounts ORDER BY account_key""")
        latest = await _fetchone(connection, """SELECT snapshot_id,as_of,cash,equity,gross_exposure,net_exposure,drawdown,payload
                     FROM quant.paper_portfolio_snapshots ORDER BY as_of DESC LIMIT 1""")
        risks = await _fetchall(connection, """SELECT risk_event_id,decision_id,symbol,event_type,severity,message,occurred_at,details
                     FROM quant.paper_risk_events ORDER BY occurred_at DESC LIMIT %s""", (bounded,))
        barriers = await _fetchall(connection, """SELECT signal_event_id,label_key,upper_return,lower_return,max_horizon_minutes,
                          entry_observed_at,entry_price,exit_observed_at,exit_price,label,raw_return,status,
                          tradability,source_status,calculated_at
                     FROM quant.paper_barrier_outcomes ORDER BY calculated_at DESC LIMIT %s""", (bounded,))
    return {"mode": "paper_research_only", "live_orders": False,
            "decisions": decisions, "positions": positions, "orders": orders, "accounts": accounts,
            "latest_portfolio": latest, "risk_events": risks, "barrier_outcomes": barriers,
            "boundary": "no_automatic_order; historical_replay_not_run"}


async def strategy_funnel(async_database: Any, limit: int = 100) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 500))
    async with async_database.transaction() as connection:
        counts = await _fetchone(connection, """SELECT
                    (SELECT count(*) FROM quant.intraday_quote_observations WHERE observed_at >= now()-interval '1 day')::int AS quote_observations,
                    (SELECT count(*) FROM quant.intraday_signal_events WHERE observed_at >= now()-interval '1 day')::int AS signal_events,
                    (SELECT count(*) FROM quant.intraday_signal_episodes WHERE first_observed_at >= now()-interval '1 day')::int AS episodes,
                    (SELECT count(*) FROM quant.intraday_signal_episodes WHERE first_observed_at >= now()-interval '1 day' AND state='active')::int AS active_episodes,
                    (SELECT count(*) FROM quant.paper_decisions WHERE decision_at >= now()-interval '1 day')::int AS paper_proposals,
                    (SELECT count(*) FROM quant.paper_decisions WHERE decision_at >= now()-interval '1 day' AND status='blocked')::int AS blocked_proposals""")
        timeline = await _fetchall(connection, """SELECT e.episode_id,e.symbol,e.strategy_key,e.strategy_version,e.direction,e.state,e.stage,
                          e.first_observed_at,e.last_observed_at,e.rearm_count,
                          count(s.signal_event_id)::int AS event_count,
                          max(s.state) FILTER (WHERE s.state='alerted') AS alert_state
                     FROM quant.intraday_signal_episodes e LEFT JOIN quant.intraday_signal_events s ON s.episode_id=e.episode_id
                    WHERE e.first_observed_at >= now()-interval '1 day'
                    GROUP BY e.episode_id ORDER BY e.last_observed_at DESC LIMIT %s""", (bounded,))
    return {"funnel": counts or {}, "episodes": timeline,
            "boundary": "automatic_candidates_are_frontend_only; paper proposals are not orders"}


async def strategy_contracts(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        rows = await _fetchall(connection, """SELECT strategy_key,strategy_version,status,contract,trial_id,approved_by,approved_at,updated_at
                     FROM quant.strategy_contracts ORDER BY strategy_key,strategy_version""")
    return {"items": rows, "live_effect": "none"}


async def strategy_governance(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        trials = await _fetchall(connection, """SELECT trial_id,strategy_key,strategy_version,status,hypothesis,data_boundary,parameters,
                          approved_by,approved_at,created_at,updated_at FROM quant.strategy_trials ORDER BY updated_at DESC""")
        contracts = await _fetchall(connection, """SELECT strategy_key,strategy_version,status,trial_id,approved_by,approved_at,updated_at
                     FROM quant.strategy_contracts ORDER BY strategy_key,strategy_version""")
    return {"trials": trials, "contracts": contracts, "live_effect": "none",
            "promotion_boundary": "research_only_until_replay_and_human_approval"}


__all__ = ["paper_status", "strategy_funnel", "strategy_contracts", "strategy_governance"]
