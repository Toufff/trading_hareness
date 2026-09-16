"""Read-only, point-in-time inputs for a private stock B/S review.

Order exports contain order time, not exact execution time.  This module never
promotes a price-cross estimate to a broker-confirmed execution timestamp.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .broker_order_timeline import map_execution_to_bars
from . import stock_sb_review_context as context


BENCHMARKS = ("000001.SH", "399001.SZ", "000300.SH", "000905.SH")
CN = ZoneInfo("Asia/Shanghai")


def json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def deep_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: deep_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_json(item) for item in value]
    return json_value(value)


def clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: json_value(value) for key, value in row.items()} for row in rows]


def normalized_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if len(value) == 6 and value.isdigit():
        if value.startswith(("4", "8", "92")):
            suffix = "BJ"
        elif value.startswith(("5", "6", "9")):
            suffix = "SH"
        elif value.startswith(("0", "1", "2", "3")):
            suffix = "SZ"
        else:
            raise ValueError("无法识别该 A 股代码的交易所")
        return f"{value}.{suffix}"
    if len(value) == 9 and value[:6].isdigit() and value[6] == "." and value[7:] in {"SH", "SZ", "BJ"}:
        return value
    raise ValueError("股票代码须为 6 位 A 股代码或 600664.SH 格式")


def _fetch(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def resolve_order_symbol(connection: Any, *, account_key: str, day: date, stock: str) -> str:
    """Resolve a name only within this account/day's imported activity."""
    try:
        return normalized_symbol(stock)
    except ValueError:
        pass
    name = stock.strip()
    if not name or len(name) > 40:
        raise ValueError("请提供有效股票名或代码")
    rows = _fetch(connection, """
        SELECT DISTINCT symbol,name FROM (
            SELECT symbol,name FROM quant.broker_order_events
             WHERE account_key=%s AND order_date=%s AND name LIKE %s
            UNION ALL
            SELECT symbol,name FROM quant.broker_trade_records
             WHERE account_key=%s AND trade_date=%s AND name LIKE %s
        ) activity ORDER BY symbol LIMIT 20
    """, (account_key, day, f"%{name}%", account_key, day, f"%{name}%"))
    symbols = sorted({row["symbol"] for row in rows if row["symbol"]})
    if len(symbols) == 1:
        return symbols[0]
    if not symbols:
        raise ValueError(f"{day} 的已导入委托中找不到“{name}”；请核对名称、账户和日期")
    raise ValueError(f"股票名“{name}”对应多个代码：{', '.join(symbols)}；请指定代码")


def _exact_fill_event(row: dict[str, Any]) -> dict[str, Any]:
    """Project one broker-confirmed execution without inventing an order size."""
    day = row["trade_date"]
    metadata = row.get("metadata") or {}
    has_order_time = bool(metadata.get("order_time"))
    order_time = time.fromisoformat(str(metadata["order_time"])) if has_order_time else row["trade_time"]
    order_at = datetime.combine(day, order_time, tzinfo=CN)
    fill_at = datetime.combine(day, row["trade_time"], tzinfo=CN)
    return {
        "event_key": row["trade_key"], "order_date": day, "order_at": order_at,
        "fill_at": fill_at, "symbol": row["symbol"], "name": row["name"],
        "side": row["side"], "raw_side": "买入" if row["side"] == "buy" else "卖出",
        "status": "成交明细", "order_number": metadata.get("order_number") or "未提供",
        "execution_number": metadata.get("execution_number") or "未提供",
        "order_quantity": None, "filled_quantity": row["quantity"],
        "gross_amount": row["gross_amount"], "order_price": None,
        "fill_price": row["price"], "is_execution": True,
        "time_basis": "broker_exact_fill_time", "source_type": "trade_fill",
        "order_time_basis": "export_order_time" if has_order_time else "fill_time_fallback",
        "source_sha256": row["source_sha256"],
    }


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


def collect_review(connection: Any, *, account_key: str, day: date, symbol: str,
                   pinned_sectors: list[str] | None = None,
                   external_benchmarks: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Collect database evidence; ``external_benchmarks`` are pre-verified review-time fetches."""
    symbol = normalized_symbol(symbol)
    pinned = [label.strip() for label in pinned_sectors or [] if label.strip()]
    order_events = _fetch(connection, """
        SELECT event_key,order_date,order_at,symbol,name,side,raw_side,status,order_number,
               order_quantity,filled_quantity,gross_amount,order_price,fill_price,
               is_execution,time_basis,source_sha256
          FROM quant.broker_order_events
         WHERE account_key=%s AND symbol=%s AND order_date=%s
         ORDER BY order_at,event_key LIMIT 300
    """, (account_key, symbol, day))
    trade_rows = _fetch(connection, """
        SELECT trade_key,trade_date,trade_time,symbol,name,side,quantity,price,gross_amount,
               source_sha256,metadata
         FROM quant.broker_trade_records
         WHERE account_key=%s AND symbol=%s AND trade_date=%s AND trade_time IS NOT NULL
         ORDER BY trade_time,trade_key LIMIT 300
    """, (account_key, symbol, day))
    trade_quantity_by_order: dict[str, Decimal] = {}
    for trade in trade_rows:
        number = str((trade.get("metadata") or {}).get("order_number") or "")
        trade_quantity_by_order[number] = trade_quantity_by_order.get(number, Decimal(0)) + Decimal(trade["quantity"])
    # A complete exact-fill export supersedes the aggregate execution row,
    # while cancelled and unmatched order rows remain visible.
    events = [event for event in order_events if not (
        event["is_execution"] and event["order_number"] in trade_quantity_by_order
        and trade_quantity_by_order[event["order_number"]] == Decimal(event["filled_quantity"])
    )]
    events.extend(_exact_fill_event(row) for row in trade_rows)
    events.sort(key=lambda row: (row.get("fill_at") or row["order_at"], row["event_key"]))
    if not events:
        raise ValueError(f"该账户在 {day} 没有 {symbol} 的已导入委托或成交记录；先确认导入日期和账户绑定")

    bars, bar_source = _minute_rows(connection, symbol, day)
    daily = _daily_rows(connection, symbol, day)
    benchmarks: dict[str, Any] = {}
    for benchmark in BENCHMARKS:
        benchmark_bars, source = _minute_rows(connection, benchmark, day)
        fetched = (external_benchmarks or {}).get(benchmark) or {}
        if not benchmark_bars and fetched.get("bars"):
            benchmark_bars, source = fetched["bars"], fetched["source"]
        benchmark_daily = _daily_rows(connection, benchmark, day, 10, 1)
        benchmarks[benchmark] = {"source": source, "bars": clean_rows(benchmark_bars),
                                 "fetch_status": fetched.get("source"),
                                 "reference_close": context.reference_close(benchmark_daily, day),
                                 "daily": clean_rows(benchmark_daily)}

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

    account = context.build_position_path(
        context.fetch_account_snapshots(connection, account_key=account_key, symbol=symbol, day=day),
        events, previous_trading_day=context.previous_trading_day(daily, day))
    plans = context.dedupe_plans(context.fetch_trade_plans(connection, symbol=symbol, day=day))
    book = context.compact_order_book(context.fetch_order_book(connection, symbol=symbol, day=day))
    boards = context.compact_board_snapshots(context.fetch_board_snapshots(connection, day=day))
    breadth = context.dedupe_breadth(context.fetch_market_breadth(connection, day=day))
    signals = context.fetch_signals(connection, symbol=symbol, day=day)
    known_labels = {label for label, _ in boards["labels"]}

    reviewed_events = []
    for event in events:
        row = {key: json_value(value) for key, value in event.items()}
        row["decision_context"] = _event_context(bars, event["order_at"])
        # Decision-time context is cut at the order time, not the later fill.
        decided_at = event["order_at"]
        row["position"] = deep_json(account["events"].get(event["event_key"]))
        row["active_plan_index"] = context.active_plan_index(plans, decided_at)
        row["order_book_context"] = deep_json(context.order_book_context(book, decided_at))
        row["board_context"] = deep_json(context.board_context(boards, decided_at, pinned))
        row["breadth_context"] = deep_json(context.latest_available(breadth, decided_at))
        if event["is_execution"]:
            if event.get("fill_at"):
                row["minute_mapping"] = {"exact_fill_at": json_value(event["fill_at"]),
                                         "confidence": "券商成交明细"}
            else:
                row["minute_mapping"] = map_execution_to_bars(
                    {"order_at": event["order_at"], "price": event["fill_price"]}, bars)
            row["hindsight_from_order_time"] = _post_event(
                bars, event.get("fill_at") or event["order_at"], float(event["fill_price"] or 0))
        reviewed_events.append(row)

    gaps = []
    if len(bars) < 180:
        gaps.append(f"个股分钟线仅 {len(bars)} 根；不足以作完整日内量价复盘")
    if bars and any(event.get("fill_at") and event["fill_at"] > bars[-1]["bar_time"] + timedelta(minutes=1)
                    for event in events):
        gaps.append("有真实成交时刻晚于最后一根可用分钟K；图上按成交时刻和成交价标点，但该时刻附近的量价走势缺失")
    if not any(item["bars"] for item in benchmarks.values()):
        fetch_notes = sorted({str(item["fetch_status"]) for item in benchmarks.values() if item["fetch_status"]})
        gaps.append("基准指数分钟线缺失；无法复原下单时相对大盘走势"
                    + (f"（补采结果：{'、'.join(fetch_notes)}）" if fetch_notes else ""))
    if not quotes:
        gaps.append("该股当日盘中资金/换手快照缺失；不推断当时主力资金")
    elif not any(row["main_net_inflow"] is not None for row in quotes):
        gaps.append("有盘中报价，但没有带主力净流入值的快照")
    if len(quotes) == 5000:
        gaps.append("盘中快照达到 5000 条读取上限；晚间数据可能未覆盖")
    if not sectors and boards["snapshots"]:
        pinned_note = f"和人工指定板块（{'、'.join(pinned)}），不是系统归属" if pinned else "，未指定关注板块"
        gaps.append("该股行业/概念归属未入库；板块面板只展示全市场盘中快照" + pinned_note)
    elif not sectors:
        gaps.append("没有可核查的当日行业/概念归属及对应板块表现")
    if not boards["snapshots"]:
        gaps.append("当日没有板块盘中资金快照")
    missing_pinned = [label for label in pinned if label not in known_labels]
    if missing_pinned:
        gaps.append(f"指定板块在当日快照中不存在：{'、'.join(missing_pinned)}")
    if not breadth:
        gaps.append("当日没有市场宽度（概念板块涨跌占比）分钟快照")
    if not book:
        gaps.append("当日没有盘口五档快照；无法还原下单时买卖挂单")
    if account["status"] == "missing":
        gaps.append("找不到首笔操作前的券商持仓快照；交易前仓位无法核对")
    elif account["status"] in {"stale", "unverified_age"}:
        gaps.append("交易前持仓快照早于上一交易日收盘或无法确认时点；期间若有未导入成交，推算仓位会失真")
    mismatches = [item for item in account["reconciliation"] if not item["match"]]
    if mismatches:
        gaps.append(f"{len(mismatches)} 个盘中持仓快照与“快照+已导入成交”推算不一致；可能有成交未导入")
    for field, label, collected in (("order_book_context", "盘口", book), ("board_context", "板块", boards["snapshots"])):
        early = sum(row[field]["status"] == "missing" for row in reviewed_events)
        if early and collected:
            gaps.append(f"{early} 笔操作早于当日首个{label}快照；这些操作的{label}当时状态无法还原")
    early_breadth = sum(row["breadth_context"] is None for row in reviewed_events)
    if early_breadth and breadth:
        gaps.append(f"{early_breadth} 笔操作早于当日首个市场宽度快照")
    if all(row["active_plan_index"] is None for row in reviewed_events):
        gaps.append("所有操作时点都没有已记录且仍有效的持仓计划")
    if not moneyflow:
        gaps.append("近 45 天该股资金流数据缺失")
    if not daily:
        gaps.append("该股日线背景缺失")
    return {
        "version": 2, "account_key": account_key, "day": day.isoformat(), "symbol": symbol,
        "name": events[0]["name"], "time_semantics": (
            "当日成交导出中的成交时刻为券商记录的事实；委托流水若无成交时刻，仍只作价格触达推断"
            if trade_rows else "委托时间为事实；成交分钟仅由委托后成交价首次触达推断，不是实际成交时间"),
        "bar_source": bar_source, "coverage": {"stock_minute_bars": len(bars),
        "index_minute_bars": {key: len(value["bars"]) for key, value in benchmarks.items()},
        "quotes": len(quotes), "exact_fill_rows": len(trade_rows),
        "quotes_with_main_net_inflow": sum(row["main_net_inflow"] is not None for row in quotes),
        "account_snapshots": len(account["snapshots"]), "position_baseline": account["status"],
        "trade_plans": len(plans), "order_book_rows": len(book), "board_snapshots": len(boards["snapshots"]),
        "breadth_minutes": len(breadth), "signals": len(signals),
        "benchmark_sources": {key: value["source"] for key, value in benchmarks.items()},
        "gaps": gaps},
        "events": reviewed_events, "bars": clean_rows(bars), "daily": clean_rows(daily),
        "benchmarks": benchmarks, "moneyflow": clean_rows(moneyflow), "sectors": clean_rows(sectors),
        "market": clean_rows(market), "news": clean_rows(news), "quotes": clean_rows(quotes),
        "reference_close": context.reference_close(daily, day), "pinned_sectors": pinned,
        "account": deep_json(account), "plans": deep_json(plans), "order_book": deep_json(book),
        "order_book_minutes": deep_json(context.order_book_minutes(book)), "boards": deep_json(boards),
        "breadth": deep_json(breadth), "signals": clean_rows(signals),
    }
