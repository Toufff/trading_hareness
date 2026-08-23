"""Native-async, evidence-only analyst-versus-market evaluation reads."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .analyst_market_evaluation import CN, summarize_evaluation


async def market_evaluation(
    async_database: Any,
    start_date: date | None = None,
    end_date: date | None = None,
    analyst_id: str | None = None,
) -> dict[str, Any]:
    """Project existing point-in-time ledgers without fetching or writing data."""
    end = end_date or datetime.now(CN).date()
    start = start_date or (end - timedelta(days=14))
    if end < start or (end - start).days > 62:
        raise ValueError("evaluation window must be ordered and no longer than 62 days")
    async with async_database.transaction() as connection:
        observations_result = await connection.execute(
            """SELECT analyst_id,source_kind,strategy_available_at,scope,action,direction,status,subject_key,subject_label
                 FROM quant.analyst_observations
                WHERE strategy_available_at >= %s::date AND strategy_available_at < (%s::date + interval '1 day')
                  AND (%s::text IS NULL OR analyst_id=%s)""", (start, end, analyst_id, analyst_id),
        )
        opinions_result = await connection.execute(
            """SELECT remote_analyst_id,opinion_date,scope,subject_key,direction,strength,factor_status
                 FROM quant.analyst_opinions
                WHERE opinion_date BETWEEN %s AND %s AND (%s::text IS NULL OR remote_analyst_id=%s)""",
            (start, end, analyst_id, analyst_id),
        )
        outcomes_result = await connection.execute(
            """SELECT o.opinion_id,p.remote_analyst_id,p.opinion_date,p.scope,p.subject_key,
                            p.direction * p.strength * p.explicitness score,
                            o.status,o.directional_return,o.residual_return,o.horizon_days
                 FROM quant.analyst_opinion_outcomes o JOIN quant.analyst_opinions p ON p.opinion_id=o.opinion_id
                WHERE p.opinion_date BETWEEN %s AND %s AND (%s::text IS NULL OR p.remote_analyst_id=%s)""",
            (start, end, analyst_id, analyst_id),
        )
        intraday_result = await connection.execute(
            """SELECT io.observation_id,ob.analyst_id,ob.scope,ob.subject_key,ob.action,ob.direction,
                          io.horizon_minutes,io.status,io.directional_return,io.settlement
                 FROM quant.analyst_intraday_outcomes io JOIN quant.analyst_observations ob ON ob.observation_id=io.observation_id
                WHERE (ob.strategy_available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND (%s::text IS NULL OR ob.analyst_id=%s)""", (start, end, analyst_id, analyst_id),
        )
        author_actions_result = await connection.execute(
            """SELECT a.remote_analyst_id AS analyst_id,a.action_type,ao.horizon_minutes,ao.status,ao.directional_return
                 FROM quant.analyst_action_intraday_outcomes ao
                 JOIN quant.analyst_trade_actions a ON a.action_id=ao.action_id
                WHERE (a.stated_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND (%s::text IS NULL OR a.remote_analyst_id=%s)""", (start, end, analyst_id, analyst_id),
        )
        market_days_result = await connection.execute(
            """SELECT DISTINCT ON(exchange_date) exchange_date,market_state,status,concept_positive_ratio,market_amount,
                            amount_change_pct,advancer_ratio
                 FROM quant.market_flow_feature_snapshots
                WHERE exchange_date BETWEEN %s AND %s AND cadence IN ('close','midday')
                ORDER BY exchange_date,CASE cadence WHEN 'close' THEN 0 ELSE 1 END,observed_at DESC""", (start, end),
        )
        sector_days_result = await connection.execute(
            """SELECT feature.sector_key,sector.label,feature.net_amount,feature.lhb_negative_count,feature.trading_date
                 FROM quant.sector_flow_daily_features feature JOIN quant.sectors sector
                   ON sector.taxonomy_key=feature.taxonomy_key AND sector.sector_key=feature.sector_key
                WHERE feature.taxonomy_key='ths_concept_flow' AND trading_date BETWEEN %s AND %s""", (start, end),
        )
        aliases_result = await connection.execute(
            """SELECT theme_key,sector_key
                 FROM quant.analyst_theme_board_aliases
                WHERE status='approved' AND taxonomy_key='ths_concept_flow'"""
        )
        observations = [dict(row) for row in await observations_result.fetchall()]
        opinions = [dict(row) for row in await opinions_result.fetchall()]
        outcomes = [dict(row) for row in await outcomes_result.fetchall()]
        intraday_outcomes = [dict(row) for row in await intraday_result.fetchall()]
        author_action_outcomes = [dict(row) for row in await author_actions_result.fetchall()]
        market_days = [dict(row) for row in await market_days_result.fetchall()]
        sector_days = [dict(row) for row in await sector_days_result.fetchall()]
        theme_board_map = {str(row["theme_key"]): str(row["sector_key"])
                           for row in await aliases_result.fetchall()}
    result = summarize_evaluation(
        observations=observations, opinions=opinions, outcomes=outcomes,
        intraday_outcomes=intraday_outcomes, author_action_outcomes=author_action_outcomes,
        market_days=market_days, sector_days=sector_days, market_days_for_baseline=market_days,
        theme_board_map=theme_board_map, start_date=start, end_date=end,
    )
    result["analyst_id"] = analyst_id
    return result


__all__ = ["market_evaluation"]
