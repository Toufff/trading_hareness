"""Read-only, point-in-time inputs for a private stock B/S review.

Order exports contain order time, not exact execution time.  This module never
promotes a price-cross estimate to a broker-confirmed execution timestamp.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .broker_order_timeline import map_execution_to_bars


BENCHMARKS = ("000001.SH", "399001.SZ", "000300.SH", "000905.SH")


def json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: json_value(value) for key, value in row.items()} for row in rows]


def normalized_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if len(value) == 6 and value.isdigit():
        suffix = "SH" if value.startswith(("5", "6", "9")) else "SZ" if value.startswith(("0", "1", "2", "3")) else "BJ"
        return f"{value}.{suffix}"
    if len(value) == 9 and value[:6].isdigit() and value[6] == "." and value[7:] in {"SH", "SZ", "BJ"}:
        return value
    raise ValueError("股票代码须为 6 位 A 股代码或 600664.SH 格式")


def _fetch(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _minute_rows(connection: Any, symbol: str, day: date) -> tuple[list[dict[str, Any]], str]:
    bars = _fetch(connection, """
        SELECT bar_time,open,high,low,close,volume,amount,source_name,available_at
          FROM quant.intraday_minute_sessions WHERE symbol=%s AND trading_date=%s
         ORDER BY bar_time LIMIT 400
    """, (symbol, day))
    if bars:
        return bars, "intraday_minute_sessions"
    bars = _fetch(connection, """
        SELECT bar_time,open,high,low,close,volume,amount,source_name,available_at
          FROM quant.market_bars_minute
         WHERE symbol=%s AND bar_time >= %s::date AND bar_time < (%s::date + interval '1 day')
         ORDER BY bar_time LIMIT 400
    """, (symbol, day, day))
    return bars, "market_bars_minute" if bars else "missing"


def _daily_rows(connection: Any, symbol: str, day: date, before: int = 65, after: int = 7) -> list[dict[str, Any]]:
    return _fetch(connection, """
        SELECT trading_date,open,high,low,close,pre_close,volume,amount,selected_provider,
               quality_status,available_at
          FROM quant.canonical_bars_daily
         WHERE symbol=%s AND trading_date BETWEEN %s AND %s
         ORDER BY trading_date LIMIT 100
    """, (symbol, day - timedelta(days=before * 2), day + timedelta(days=after)))


def _event_context(bars: list[dict[str, Any]], order_at: datetime) -> dict[str, Any]:
    """Only minute rows at or before the order may enter the decision-time view."""
    # A minute stamp denotes the start of its interval.  Its close/high/low
    # are only fully known after the minute ends, not at an order inside it.
    history = [row for row in bars if row["bar_time"] + timedelta(minutes=1) <= order_at]
    if not history:
        return {"minute_count": 0, "last_price": None, "turnover_weighted_close_so_far": None,
                "range_position_so_far": None, "last_5_amount": None, "last_15_amount": None}
    last = history[-1]
    amounts = [float(row["amount"] or 0) for row in history]
    amount_sum = sum(amounts)
    high = max(float(row["high"]) for row in history)
    low = min(float(row["low"]) for row in history)
    close = float(last["close"])
    return {
        "minute_count": len(history), "last_price": close,
        "turnover_weighted_close_so_far": round(sum(float(row["close"]) * float(row["amount"] or 0)
                                           for row in history) / amount_sum, 4) if amount_sum > 0 else None,
        "range_position_so_far": round((close - low) / (high - low), 3) if high > low else None,
        "last_5_amount": sum(amounts[-5:]), "last_15_amount": sum(amounts[-15:]),
    }


def _post_event(bars: list[dict[str, Any]], order_at: datetime, price: float) -> dict[str, Any]:
    """Hindsight is intentionally separate from decision-time context."""
    after = [row for row in bars if row["bar_time"] > order_at]
    output: dict[str, Any] = {}
    if price <= 0:
        return output
    for count in (5, 15, 30, 60):
        if len(after) >= count:
            window = after[:count]
            output[str(count)] = {
                "close_return_pct": round((float(window[-1]["close"]) / price - 1) * 100, 2),
                "max_up_pct": round((max(float(row["high"]) for row in window) / price - 1) * 100, 2),
                "max_down_pct": round((min(float(row["low"]) for row in window) / price - 1) * 100, 2),
            }
    return output


def collect_review(connection: Any, *, account_key: str, day: date, symbol: str) -> dict[str, Any]:
    symbol = normalized_symbol(symbol)
    events = _fetch(connection, """
        SELECT event_key,order_date,order_at,symbol,name,side,raw_side,status,order_number,
               order_quantity,filled_quantity,gross_amount,order_price,fill_price,
               is_execution,time_basis,source_sha256
          FROM quant.broker_order_events
         WHERE account_key=%s AND symbol=%s AND order_date=%s
         ORDER BY order_at,event_key LIMIT 300
    """, (account_key, symbol, day))
    if not events:
        raise ValueError(f"该账户在 {day} 没有 {symbol} 的已导入委托记录；先确认导入日期和账户绑定")

    bars, bar_source = _minute_rows(connection, symbol, day)
    daily = _daily_rows(connection, symbol, day)
    benchmarks: dict[str, Any] = {}
    for benchmark in BENCHMARKS:
        benchmark_bars, source = _minute_rows(connection, benchmark, day)
        benchmarks[benchmark] = {"source": source, "bars": clean_rows(benchmark_bars),
                                 "daily": clean_rows(_daily_rows(connection, benchmark, day, 10, 1))}

    moneyflow = _fetch(connection, """
        SELECT trading_date,source,provider,net_amount,net_amount_rate,available_at
          FROM quant.stock_money_flow_daily
         WHERE symbol=%s AND trading_date BETWEEN %s AND %s
         ORDER BY trading_date DESC,source LIMIT 45
    """, (symbol, day - timedelta(days=45), day))
    sectors = _fetch(connection, """
        SELECT h.taxonomy_key,h.sector_key,s.label,h.effective_from,h.effective_to,
               h.effective_from_basis,h.available_at AS membership_available_at,
               o.trading_date,o.change_pct,o.net_amount,o.available_at AS observation_available_at
          FROM quant.sector_membership_history h
          JOIN quant.sectors s ON s.taxonomy_key=h.taxonomy_key AND s.sector_key=h.sector_key
          LEFT JOIN LATERAL (
              SELECT trading_date,change_pct,net_amount,available_at
                FROM quant.sector_market_observations
               WHERE taxonomy_key=h.taxonomy_key AND sector_key=h.sector_key
                 AND trading_date<=%s ORDER BY trading_date DESC LIMIT 1
          ) o ON true
         WHERE h.symbol=%s AND h.effective_from<=%s
           AND (h.effective_to IS NULL OR h.effective_to>=%s)
         ORDER BY h.taxonomy_key,h.sector_key LIMIT 35
    """, (day, symbol, day, day))
    market = _fetch(connection, """
        SELECT trading_date,stock_count,advancers,decliners,median_change_pct,total_amount_kcny,
               source_provider,available_at
          FROM quant.daily_market_aggregates
         WHERE trading_date BETWEEN %s AND %s
         ORDER BY trading_date DESC LIMIT 10
    """, (day - timedelta(days=18), day))
    news = _fetch(connection, """
        SELECT occurred_at,available_at,event_type,source,title,url
          FROM quant.market_events
         WHERE symbol=%s AND available_at BETWEEN %s AND %s
         ORDER BY available_at DESC LIMIT 25
    """, (symbol, day - timedelta(days=15), day + timedelta(days=2)))
    quotes = _fetch(connection, """
        SELECT observed_at,source_name,price,pct_change,volume_ratio,turnover_rate,main_net_inflow
          FROM quant.intraday_quote_observations
         WHERE symbol=%s AND observed_at >= %s::date AND observed_at < (%s::date + interval '1 day')
         ORDER BY observed_at LIMIT 5000
    """, (symbol, day, day))

    reviewed_events = []
    for event in events:
        row = {key: json_value(value) for key, value in event.items()}
        row["decision_context"] = _event_context(bars, event["order_at"])
        if event["is_execution"]:
            row["minute_mapping"] = map_execution_to_bars(
                {"order_at": event["order_at"], "price": event["fill_price"]}, bars)
            row["hindsight_from_order_time"] = _post_event(
                bars, event["order_at"], float(event["fill_price"] or 0))
        reviewed_events.append(row)

    gaps = []
    if len(bars) < 180:
        gaps.append(f"个股分钟线仅 {len(bars)} 根；不足以作完整日内量价复盘")
    if not any(item["bars"] for item in benchmarks.values()):
        gaps.append("基准指数分钟线缺失；无法复原下单时相对大盘走势")
    if not quotes:
        gaps.append("该股当日盘中资金/换手快照缺失；不推断当时主力资金")
    elif not any(row["main_net_inflow"] is not None for row in quotes):
        gaps.append("有盘中报价，但没有带主力净流入值的快照")
    if len(quotes) == 5000:
        gaps.append("盘中快照达到 5000 条读取上限；晚间数据可能未覆盖")
    if not sectors:
        gaps.append("没有可核查的当日行业/概念归属及对应板块表现")
    if not moneyflow:
        gaps.append("近 45 天该股资金流数据缺失")
    if not daily:
        gaps.append("该股日线背景缺失")
    return {
        "version": 1, "account_key": account_key, "day": day.isoformat(), "symbol": symbol,
        "name": events[0]["name"], "time_semantics": "委托时间为事实；成交分钟仅由委托后成交价首次触达推断，不是实际成交时间",
        "bar_source": bar_source, "coverage": {"stock_minute_bars": len(bars),
        "index_minute_bars": {key: len(value["bars"]) for key, value in benchmarks.items()},
        "quotes": len(quotes),
        "quotes_with_main_net_inflow": sum(row["main_net_inflow"] is not None for row in quotes),
        "gaps": gaps},
        "events": reviewed_events, "bars": clean_rows(bars), "daily": clean_rows(daily),
        "benchmarks": benchmarks, "moneyflow": clean_rows(moneyflow), "sectors": clean_rows(sectors),
        "market": clean_rows(market), "news": clean_rows(news), "quotes": clean_rows(quotes),
    }
