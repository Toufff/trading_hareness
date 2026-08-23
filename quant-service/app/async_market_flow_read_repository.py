"""Native-async projection for stored multiscale market-flow evidence."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .market_flow_read_model import project_market_flow_features


async def market_flow_features(async_database: Any, trade_date: date | None = None, *, limit: int = 720) -> dict[str, Any]:
    china = ZoneInfo("Asia/Shanghai")
    selected_date = trade_date or datetime.now(timezone.utc).astimezone(china).date()
    bounded_limit = max(1, min(int(limit), 1000))
    async with async_database.transaction() as connection:
        rows_result = await connection.execute(
            """SELECT feature_key,exchange_date,cadence,observed_at,source_snapshot_minute,status,market_state,
                      concept_count,concept_positive_ratio,concept_median_flow,concept_mean_change_pct,
                      five_minute_positive_ratio_delta,session_positive_ratio_delta,afternoon_repair_strength,
                      market_amount,market_volume,amount_change_pct,volume_change_pct,advancer_ratio,
                      features,quality_flags,updated_at
                 FROM quant.market_flow_feature_snapshots
                WHERE exchange_date=%s
                ORDER BY observed_at LIMIT %s""", (selected_date, bounded_limit),
        )
        daily_result = await connection.execute(
            """SELECT DISTINCT ON(exchange_date) feature_key,exchange_date,cadence,observed_at,status,market_state,
                      concept_count,concept_positive_ratio,market_amount,market_volume,amount_change_pct,
                      volume_change_pct,advancer_ratio,features,quality_flags
                 FROM quant.market_flow_feature_snapshots
                WHERE cadence IN ('close','midday')
                ORDER BY exchange_date DESC,
                         CASE cadence WHEN 'close' THEN 0 ELSE 1 END,observed_at DESC
                LIMIT 20"""
        )
        sector_result = await connection.execute(
            """SELECT feature.trading_date,feature.sector_key,sector.label,feature.provider_key,
                      feature.status,feature.transition,feature.net_amount,feature.previous_net_amount,
                      feature.net_change_amount,feature.net_acceleration,feature.rank_percentile,
                      feature.flow_sign_streak,feature.change_pct,feature.price_flow_divergence,
                      feature.lhb_stock_count,feature.lhb_net_amount,feature.lhb_negative_count,
                      feature.lhb_sell_pressure_ratio,feature.limit_up_count,feature.quality_flags
                 FROM quant.sector_flow_daily_features feature
                 JOIN quant.sectors sector
                   ON sector.taxonomy_key=feature.taxonomy_key AND sector.sector_key=feature.sector_key
                WHERE feature.taxonomy_key='ths_concept_flow' AND feature.trading_date=%s
                ORDER BY feature.rank_percentile DESC NULLS LAST,abs(feature.net_change_amount) DESC NULLS LAST
                LIMIT 500""", (selected_date,),
        )
        outcomes_result = await connection.execute(
            """SELECT transition,horizon_days,count(*) FILTER (WHERE status='matured') AS matured,
                      avg(directional_return) FILTER (WHERE status='matured') AS avg_directional_return,
                      avg(cross_section_excess_return) FILTER (WHERE status='matured') AS avg_excess_return,
                      avg((directional_return>0)::int) FILTER (WHERE status='matured') AS directional_hit_rate
                 FROM quant.sector_flow_daily_outcomes
                GROUP BY transition,horizon_days
                ORDER BY horizon_days,transition"""
        )
        readiness_result = await connection.execute(
            """SELECT (SELECT count(DISTINCT trading_date) FROM quant.sector_flow_daily_features) AS trading_days,
                      count(DISTINCT (sector_key,signal_date)) FILTER (WHERE status='matured') AS matured_events
                 FROM quant.sector_flow_daily_outcomes"""
        )
        rows = [dict(row) for row in await rows_result.fetchall()]
        daily_rows = [dict(row) for row in await daily_result.fetchall()]
        sector_rows = [dict(row) for row in await sector_result.fetchall()]
        outcome_rows = [dict(row) for row in await outcomes_result.fetchall()]
        readiness = await readiness_result.fetchone()
    return project_market_flow_features(rows, daily_rows, sector_rows, outcome_rows, readiness or {}, selected_date)


__all__ = ["market_flow_features"]
