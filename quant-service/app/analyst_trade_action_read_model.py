"""Read-only replay view for author-timestamped analyst trade actions."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


_CN = ZoneInfo("Asia/Shanghai")


def anqiang_trade_action_replay(database: Any, as_of_date: date | None, limit: int) -> dict[str, Any]:
    """Return actions with only evidence that was actually persisted locally.

    Intraday returns are present only when our Tencent watch/order-book capture
    has a nearest observation within five minutes.  Otherwise the caller gets
    the same-day official daily bar, not a fabricated minute replay.
    """
    limit = max(1, min(int(limit), 200))
    with database.transaction() as connection:
        params: list[Any] = []
        predicate = ""
        if as_of_date is not None:
            predicate = "AND (a.stated_at AT TIME ZONE 'Asia/Shanghai')::date=%s"
            params.append(as_of_date)
        rows = connection.execute(
            f"""SELECT a.action_id,a.remote_report_id,a.symbol,a.label,a.action_type,a.direction,a.stated_at,a.available_at,
                       a.target_price,a.evidence,a.raw,
                       near.observed_at AS quote_observed_at,near.price AS quote_price,close_quote.price AS session_close_price,
                       daily.open AS daily_open,daily.high AS daily_high,daily.low AS daily_low,daily.close AS daily_close,
                       daily.pre_close AS daily_pre_close,daily.volume AS daily_volume
                  FROM quant.analyst_trade_actions a
                  LEFT JOIN LATERAL (
                    SELECT q.observed_at,q.price
                      FROM quant.intraday_quote_observations q
                     WHERE q.symbol=a.symbol AND q.source_name='tencent_free'
                       AND q.observed_at >= a.stated_at - interval '5 minutes'
                       AND q.observed_at <= a.stated_at + interval '5 minutes'
                     ORDER BY abs(extract(epoch FROM (q.observed_at-a.stated_at))) LIMIT 1
                  ) near ON true
                  LEFT JOIN LATERAL (
                    SELECT q.price FROM quant.intraday_quote_observations q
                     WHERE q.symbol=a.symbol AND q.source_name='tencent_free'
                       AND (q.observed_at AT TIME ZONE 'Asia/Shanghai')::date=(a.stated_at AT TIME ZONE 'Asia/Shanghai')::date
                     ORDER BY q.observed_at DESC LIMIT 1
                  ) close_quote ON true
                  LEFT JOIN quant.canonical_bars_daily daily
                    ON daily.symbol=a.symbol AND daily.trading_date=(a.stated_at AT TIME ZONE 'Asia/Shanghai')::date
                 WHERE a.remote_analyst_id='anqiang-touzi-riji' {predicate}
                 ORDER BY a.stated_at DESC,a.created_at DESC LIMIT %s""",
            (*params, limit),
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        quote_price = item.get("quote_price")
        close_price = item.get("session_close_price")
        daily_close = item.get("daily_close")
        if quote_price and close_price:
            item["same_session_return_pct"] = round((float(close_price) / float(quote_price) - 1) * 100, 3)
            item["evaluation_quality"] = "persisted_intraday_quote"
        elif daily_close:
            item["same_session_return_pct"] = None
            item["evaluation_quality"] = "daily_close_only"
        else:
            item["same_session_return_pct"] = None
            item["evaluation_quality"] = "awaiting_market_data"
        delay = item["available_at"] - item["stated_at"] if item.get("available_at") and item.get("stated_at") else None
        item["availability_delay_seconds"] = int(delay.total_seconds()) if delay else None
        item["factor_eligible"] = bool(delay is not None and timedelta(0) <= delay <= timedelta(minutes=5))
        item["factor_exclusion_reason"] = None if item["factor_eligible"] else "returns start at first local receipt; delayed archive action is replay-only"
        items.append(item)
    return {
        "analyst_id": "anqiang-touzi-riji", "as_of_date": str(as_of_date) if as_of_date else None,
        "items": items, "limit": limit,
        "data_boundary": "author-stated timestamps are replay evidence; any return starts at first local receipt",
    }
