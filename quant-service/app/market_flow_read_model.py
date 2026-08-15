"""Read-only projections for persisted multiscale market-flow evidence."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


def market_flow_features(
    database: Any,
    trade_date: date | None = None,
    *,
    limit: int = 720,
) -> dict[str, Any]:
    china = ZoneInfo("Asia/Shanghai")
    selected_date = trade_date or datetime.now(timezone.utc).astimezone(china).date()
    bounded_limit = max(1, min(int(limit), 1000))
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT feature_key,exchange_date,cadence,observed_at,source_snapshot_minute,status,market_state,
                      concept_count,concept_positive_ratio,concept_median_flow,concept_mean_change_pct,
                      five_minute_positive_ratio_delta,session_positive_ratio_delta,afternoon_repair_strength,
                      market_amount,market_volume,amount_change_pct,volume_change_pct,advancer_ratio,
                      features,quality_flags,updated_at
                 FROM quant.market_flow_feature_snapshots
                WHERE exchange_date=%s
                ORDER BY observed_at LIMIT %s""",
            (selected_date, bounded_limit),
        ).fetchall()
        daily_rows = connection.execute(
            """SELECT DISTINCT ON(exchange_date) feature_key,exchange_date,cadence,observed_at,status,market_state,
                      concept_count,concept_positive_ratio,market_amount,market_volume,amount_change_pct,
                      volume_change_pct,advancer_ratio,features,quality_flags
                 FROM quant.market_flow_feature_snapshots
                WHERE cadence IN ('close','midday')
                ORDER BY exchange_date DESC,
                         CASE cadence WHEN 'close' THEN 0 ELSE 1 END,observed_at DESC
                LIMIT 20""",
        ).fetchall()
        sector_rows = connection.execute(
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
                LIMIT 500""",
            (selected_date,),
        ).fetchall()
    items = [dict(row) for row in rows]
    state_counts: dict[str, int] = {}
    for item in items:
        state = str(item["market_state"])
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "trade_date": str(selected_date),
        "timezone": "Asia/Shanghai",
        "items": items,
        "latest": items[-1] if items else None,
        "daily": [dict(row) for row in daily_rows],
        "sector_daily": [dict(row) for row in sector_rows],
        "state_counts": state_counts,
        "research_gate": {
            "status": "accumulating",
            "minimum_trading_days": 60,
            "minimum_independent_events": 200,
            "live_strategy_effect": "none",
        },
        "notice": "分钟东财板块流、腾讯全A量能和盘后Tushare资金流保持分层；缺失不补零，当前仅用于研究与前端复盘。",
    }


__all__ = ["market_flow_features"]
