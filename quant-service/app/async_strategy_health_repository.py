"""Native async strategy-health read projection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .strategy_health_read_model import strategy_health_payload_from_rows


async def latest_strategy_health(async_database: Any, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    async with async_database.transaction() as connection:
        async def one(sql: str) -> Any:
            result = await connection.execute(sql)
            return await result.fetchone()

        counts = await one(
            """SELECT
                (SELECT count(*)::int FROM quant.intraday_signal_events WHERE observed_at >= now()-interval '7 days') AS signals_7d,
                (SELECT count(*)::int FROM quant.intraday_signal_events WHERE observed_at >= now()-interval '14 days' AND observed_at < now()-interval '7 days') AS signals_prior_7d,
                (SELECT count(DISTINCT episode_id)::int FROM quant.intraday_signal_events WHERE observed_at >= now()-interval '7 days' AND episode_id IS NOT NULL) AS episodes_7d,
                (SELECT count(DISTINCT episode_id)::int FROM quant.intraday_signal_events WHERE observed_at >= now()-interval '14 days' AND observed_at < now()-interval '7 days' AND episode_id IS NOT NULL) AS episodes_prior_7d,
                (SELECT count(DISTINCT e.signal_event_id)::int FROM quant.intraday_signal_outcomes o JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured' AND e.observed_at >= now()-interval '7 days') AS matured_30m_7d,
                (SELECT count(DISTINCT (e.observed_at AT TIME ZONE 'Asia/Shanghai')::date)::int FROM quant.intraday_signal_outcomes o JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured' AND e.observed_at >= now()-interval '7 days') AS matured_days_7d,
                (SELECT count(DISTINCT e.signal_event_id)::int FROM quant.intraday_signal_outcomes o JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured') AS matured_30m_total,
                (SELECT count(DISTINCT (e.observed_at AT TIME ZONE 'Asia/Shanghai')::date)::int FROM quant.intraday_signal_outcomes o JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured') AS matured_days_total"""
        )
        outcomes = await one(
            """SELECT count(DISTINCT e.signal_event_id)::int AS rows,
                      count(DISTINCT e.signal_event_id) FILTER (WHERE o.raw_return > 0)::int AS positive,
                      avg(o.raw_return) AS avg_return
                 FROM quant.intraday_signal_outcomes o
                 JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                WHERE o.horizon_key='30m' AND o.status='matured' AND e.observed_at >= now()-interval '7 days'"""
        )
        latest_quotes = await one(
            """SELECT max(observed_at) AS latest_quote_at, count(*) FILTER (WHERE observed_at >= now()-interval '90 seconds')::int AS fresh_quote_rows,
                      (SELECT is_open FROM quant.market_trade_calendar WHERE exchange='SSE'
                        AND calendar_date=(now() AT TIME ZONE 'Asia/Shanghai')::date) AS calendar_is_open
                 FROM quant.intraday_quote_observations"""
        )
        strategy_result = await connection.execute(
            """SELECT signal_key AS strategy_key,count(*)::int AS signals,
                      array_agg(DISTINCT episode_id) FILTER (WHERE episode_id IS NOT NULL) AS episode_ids
                 FROM quant.intraday_signal_events WHERE observed_at >= now()-interval '7 days'
                 GROUP BY strategy_key ORDER BY signals DESC,strategy_key"""
        )
        strategy_rows = await strategy_result.fetchall()
    return strategy_health_payload_from_rows(counts, outcomes, latest_quotes, strategy_rows, now=now)


__all__ = ["latest_strategy_health"]
