"""Native-async analyst action timeline using only local minute evidence."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .analyst_stock_timeline import CN, SYMBOL_RE, project_analyst_stock_timeline


async def stock_timeline(
    async_database: Any, *, symbol: str, start_date: date | None = None,
    end_date: date | None = None, analyst_id: str | None = None, limit: int = 1500,
) -> dict[str, Any]:
    symbol = str(symbol or "").upper().strip()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must use the 000001.SZ format")
    async with async_database.transaction() as connection:
        latest_result = await connection.execute(
            """SELECT max(trading_date) AS latest_date
                 FROM quant.intraday_minute_sessions
                WHERE symbol=%s""", (symbol,),
        )
        latest_row = await latest_result.fetchone()
        latest_available = latest_row["latest_date"] if latest_row else None
        end = end_date or latest_available or datetime.now(CN).date()
        start = start_date or end
        if end < start or (end - start).days > 31:
            raise ValueError("timeline window must be ordered and no longer than 31 days")
        bounded_limit = max(60, min(int(limit), 3000))
        bars_result = await connection.execute(
            """SELECT bar_time,open,high,low,close,volume,amount,source_name,available_at
                 FROM quant.intraday_minute_sessions
                WHERE symbol=%s AND trading_date BETWEEN %s AND %s
                ORDER BY bar_time DESC LIMIT %s""", (symbol, start, end, bounded_limit),
        )
        bars = [dict(row) for row in await bars_result.fetchall()]
        bar_source = "intraday_minute_sessions"
        if not bars:
            fallback_result = await connection.execute(
                """SELECT bar_time,open,high,low,close,volume,amount,source_name,available_at
                     FROM quant.market_bars_minute
                    WHERE symbol=%s AND (bar_time AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                    ORDER BY bar_time DESC LIMIT %s""", (symbol, start, end, bounded_limit),
            )
            bars = [dict(row) for row in await fallback_result.fetchall()]
            bar_source = "market_bars_minute"
        bars.reverse()
        actions_result = await connection.execute(
            """SELECT action_id::text AS event_id,remote_analyst_id AS analyst_id,symbol,label,
                          action_type AS action,direction,stated_at,event_time,available_at,evidence,
                          'author_trade_action' AS source_kind,true AS replay_only
                 FROM (
                    SELECT a.*,a.stated_at AS event_time
                      FROM quant.analyst_trade_actions a
                     WHERE a.symbol=%s
                       AND (a.stated_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                       AND (%s::text IS NULL OR a.remote_analyst_id=%s)
                 ) author_actions
                UNION ALL
               SELECT observation_id::text AS event_id,analyst_id,subject_key AS symbol,subject_label AS label,
                      action,direction,stated_at,coalesce(stated_at,strategy_available_at) AS event_time,
                      strategy_available_at AS available_at,evidence_span AS evidence,
                      source_kind,false AS replay_only
                 FROM quant.analyst_observations
                WHERE scope='stock' AND subject_key=%s
                  AND (coalesce(stated_at,strategy_available_at) AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND (%s::text IS NULL OR analyst_id=%s)
                ORDER BY event_time,event_id""",
            (symbol, start, end, analyst_id, analyst_id, symbol, start, end, analyst_id, analyst_id),
        )
        actions = [dict(row) for row in await actions_result.fetchall()]
    return project_analyst_stock_timeline(
        symbol=symbol, start=start, end=end, bars=bars, bar_source=bar_source, actions=actions,
    )


__all__ = ["stock_timeline"]
