"""Point-in-time decision context: what the human could see at the same moment.

Live quotes, depth and minute tape come from the same public Tencent
endpoints the platform already collects; board flow, breadth, rotations,
limit-up events, intraday signals, news research, post-close candidates and
the human's active holding plans come from the platform database, filtered to
rows available at ``now``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from ..free_market_providers import tencent_intraday_minute_session, tencent_order_book_quotes, tencent_symbol
from ..http_clients import public_http_client
from .rules import dec

SHANGHAI = ZoneInfo("Asia/Shanghai")
INDEX_SYMBOLS = {"000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指", "000688.SH": "科创50"}
DETAIL_LIMIT = 20


def _f(value: Any, digits: int = 2) -> float | None:
    number = dec(value)
    return round(float(number), digits) if value not in (None, "") else None


async def fetch_index_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    keys = [tencent_symbol(symbol) for symbol in symbols]
    async with public_http_client() as client:
        response = await client.get(f"https://qt.gtimg.cn/q={','.join(keys)}",
                                    headers={"Referer": "https://gu.qq.com", "User-Agent": "Mozilla/5.0"}, timeout=8)
    output: dict[str, dict[str, Any]] = {}
    by_key = dict(zip((key.lower() for key in keys), symbols, strict=True))
    for match in re.finditer(r'v_([a-z0-9]+)="([^"]*)";', response.text, re.I):
        symbol, fields = by_key.get(match.group(1).lower()), match.group(2).split("~")
        if not symbol or len(fields) < 5:
            continue
        price, pre_close = dec(fields[3]), dec(fields[4])
        if price > 0 and pre_close > 0:
            output[symbol] = {"price": float(price), "pct": round(float((price / pre_close - 1) * 100), 2)}
    return output


async def fetch_live_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Batched live depth quotes; failures leave a symbol absent, never guessed."""
    output: dict[str, dict[str, Any]] = {}
    unique = list(dict.fromkeys(symbols))
    for start in range(0, len(unique), 60):
        batch = unique[start:start + 60]
        try:
            rows = await tencent_order_book_quotes(batch, max_symbols=60)
        except Exception:  # noqa: BLE001 - absent quotes fail closed downstream
            continue
        output.update({row["ts_code"]: row for row in rows})
    return output


def compact_quote(row: dict[str, Any]) -> dict[str, Any]:
    pre_close = dec(row.get("pre_close"))
    price = dec(row.get("price"))
    return {
        "name": row.get("name"), "price": float(price),
        "pct": round(float((price / pre_close - 1) * 100), 2) if pre_close > 0 else None,
        "bids": [[_f(level["price"]), int(dec(level["size"]))] for level in row.get("bids") or []],
        "asks": [[_f(level["price"]), int(dec(level["size"]))] for level in row.get("asks") or []],
        "outer_minus_inner_lot": int(dec(row.get("outer_volume_lot")) - dec(row.get("inner_volume_lot"))),
        "cum_amount_million": _f(dec(row.get("cumulative_amount")) / 1_000_000, 1) if row.get("cumulative_amount") else None,
        "quote_time": row.get("trade_time"),
    }


def summarize_minutes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Session OHLC/VWAP plus recent minutes and 5-minute buckets, amounts in million yuan."""
    done = [row for row in rows if row.get("volume_lot", 0) >= 0]
    if not done:
        return {"status": "empty"}
    prices = [row["close"] for row in done]
    last = done[-1]
    buckets: list[list[Any]] = []
    for index in range(0, len(done), 5):
        chunk = done[index:index + 5]
        buckets.append([chunk[0]["time"], chunk[-1]["close"], max(r["close"] for r in chunk), min(r["close"] for r in chunk),
                        round(sum(r["amount"] for r in chunk) / 1_000_000, 1)])
    return {
        "open": prices[0], "high": max(prices), "low": min(prices), "last": last["close"],
        "high_time": done[prices.index(max(prices))]["time"], "low_time": done[prices.index(min(prices))]["time"],
        "vwap": round(last["vwap"], 3) if last.get("vwap") else None,
        "last_10_minutes": [[r["time"], r["close"], round(r["amount"] / 1_000_000, 1), r["volume_lot"]] for r in done[-10:]],
        "five_minute_buckets_time_close_high_low_amount": buckets[-24:],
        "note": "high/low are minute closes, not tick extremes",
    }


async def fetch_minutes(symbols: list[str], day: date) -> dict[str, dict[str, Any]]:
    semaphore = asyncio.Semaphore(4)

    async def one(symbol: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                session = await tencent_intraday_minute_session(symbol)
            except Exception:  # noqa: BLE001
                return symbol, {"status": "fetch_failed"}
        if session.get("session_date") != day.isoformat():
            return symbol, {"status": f"provider_session_{session.get('session_date')}"}
        return symbol, summarize_minutes(session["rows"])

    return dict(await asyncio.gather(*(one(symbol) for symbol in symbols)))


def _rows(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def daily_bars(connection: Any, symbols: list[str], day: date, count: int = 20) -> dict[str, list[list[Any]]]:
    rows = _rows(connection, """
        SELECT symbol,trading_date,open,high,low,close,pre_close,amount FROM (
          SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
            FROM quant.canonical_bars_daily WHERE symbol = ANY(%s) AND trading_date < %s) ranked
         WHERE rn <= %s ORDER BY symbol,trading_date""", (symbols, day, count))
    output: dict[str, list[list[Any]]] = {}
    for row in rows:
        pre = dec(row["pre_close"])
        output.setdefault(row["symbol"], []).append([
            row["trading_date"].isoformat()[5:], _f(row["open"]), _f(row["high"]), _f(row["low"]), _f(row["close"]),
            round(float((dec(row["close"]) / pre - 1) * 100), 2) if pre > 0 else None,
            _f(dec(row["amount"]) / 100_000, 1) if row["amount"] is not None else None,
        ])
    return output


def board_flow(connection: Any, now: datetime, day: date) -> dict[str, Any]:
    row = connection.execute(
        """SELECT observed_at,payload->'items' AS items,payload->>'unit' AS unit FROM quant.intraday_board_flow_snapshots
            WHERE observed_at<=%s AND observed_at>=%s ORDER BY observed_at DESC LIMIT 1""",
        (now, datetime.combine(day, datetime.min.time(), SHANGHAI)),
    ).fetchone()
    if row is None:
        return {"status": "none_today"}
    by_taxonomy: dict[str, list[dict[str, Any]]] = {}
    for item in row["items"] or []:
        by_taxonomy.setdefault(item.get("taxonomy_key") or "unknown", []).append(item)
    output: dict[str, Any] = {"observed_at": row["observed_at"].astimezone(SHANGHAI).strftime("%H:%M:%S"), "net_inflow_unit": row["unit"]}
    for taxonomy, items in by_taxonomy.items():
        valid = [i for i in items if i.get("change_pct") is not None]
        up = sorted(valid, key=lambda i: i["change_pct"], reverse=True)
        flow = sorted([i for i in items if i.get("net_inflow") is not None], key=lambda i: i["net_inflow"], reverse=True)
        output[taxonomy] = {
            "count": len(items), "up_ratio": round(sum(1 for i in valid if i["change_pct"] > 0) / max(1, len(valid)), 2),
            "top_change": [[i["label"], i["change_pct"], i.get("net_inflow")] for i in up[:8]],
            "bottom_change": [[i["label"], i["change_pct"], i.get("net_inflow")] for i in up[-5:]],
            "top_inflow": [[i["label"], i.get("change_pct"), i["net_inflow"]] for i in flow[:6]],
            "top_outflow": [[i["label"], i.get("change_pct"), i["net_inflow"]] for i in flow[-4:]],
        }
    return output


def breadth(connection: Any, now: datetime, day: date) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT source_snapshot_minute,market_state,concept_positive_ratio,concept_mean_change_pct,concept_median_flow,
                  five_minute_positive_ratio_delta,session_positive_ratio_delta
             FROM quant.market_flow_feature_snapshots
            WHERE exchange_date=%s AND cadence='minute' AND GREATEST(observed_at,created_at)<=%s
            ORDER BY source_snapshot_minute DESC LIMIT 1""", (day, now),
    ).fetchone()
    if row is None:
        return None
    return {key: (_f(value, 3) if isinstance(value, Decimal) else value.astimezone(SHANGHAI).strftime("%H:%M")
                  if isinstance(value, datetime) else value) for key, value in dict(row).items()}


def rotations(connection: Any, now: datetime, day: date) -> list[list[Any]]:
    rows = _rows(connection, """
        SELECT snapshot_minute,label,taxonomy_key,event_type,direction,state,conditions->>'change_pct' AS chg,
               conditions->>'current_net_inflow' AS flow
          FROM quant.intraday_board_rotation_events
         WHERE snapshot_minute>=%s AND first_observed_at<=%s AND state IN ('confirming','confirmed')
         ORDER BY snapshot_minute DESC LIMIT 12""", (datetime.combine(day, datetime.min.time(), SHANGHAI), now))
    return [[r["snapshot_minute"].astimezone(SHANGHAI).strftime("%H:%M"), r["label"], r["event_type"], r["direction"],
             r["state"], r["chg"], r["flow"]] for r in rows]


def limit_events(connection: Any, now: datetime, day: date) -> dict[str, Any]:
    start = datetime.combine(day, datetime.min.time(), SHANGHAI)
    counts = {r["event_type"]: r["n"] for r in _rows(connection, """
        SELECT event_type,count(DISTINCT symbol) AS n FROM quant.market_events
         WHERE available_at>=%s AND available_at<=%s AND event_type IN ('limit_up_pool','limit_open_pool','limit_chain')
         GROUP BY 1""", (start, now))}
    chains = _rows(connection, """
        SELECT DISTINCT ON (symbol) symbol,title FROM quant.market_events
         WHERE available_at>=%s AND available_at<=%s AND event_type='limit_chain'
         ORDER BY symbol,available_at DESC LIMIT 30""", (start, now))
    return {"distinct_symbols_today": counts, "limit_chain_titles": [r["title"] for r in chains]}


def signals(connection: Any, now: datetime, day: date) -> list[list[Any]]:
    rows = _rows(connection, """
        SELECT observed_at,symbol,signal_type,state,stage,score FROM quant.intraday_signal_events
         WHERE observed_at>=%s AND observed_at<=%s ORDER BY observed_at DESC LIMIT 12""",
                 (datetime.combine(day, datetime.min.time(), SHANGHAI), now))
    return [[r["observed_at"].astimezone(SHANGHAI).strftime("%H:%M"), r["symbol"], r["signal_type"], r["state"], r["stage"],
             _f(r["score"], 1)] for r in rows]


def news(connection: Any, now: datetime) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT created_at,result FROM quant.event_research_runs
            WHERE status='analyzed' AND created_at<=%s ORDER BY created_at DESC LIMIT 1""", (now,),
    ).fetchone()
    if row is None:
        return None
    result = row["result"] or {}
    return {
        "published_at": row["created_at"].astimezone(SHANGHAI).strftime("%m-%d %H:%M"),
        "summary": result.get("summary"),
        "events": [{"fact": e.get("fact"), "action": e.get("action"), "horizon": e.get("horizon")} for e in (result.get("events") or [])[:6]],
        "leads": [[lead.get("title"), [s.get("symbol") for s in lead.get("symbols") or [] if isinstance(s, dict)]]
                  for lead in (result.get("leads") or [])[:25]],
    }


def plans(connection: Any, now: datetime) -> list[dict[str, Any]]:
    rows = _rows(connection, """
        SELECT DISTINCT ON (symbol) symbol,name,plan_kind,action,entry_zone,add_trigger,reduce_trigger,exit_trigger,
               stop_price,target_prices,max_position_pct,rationale,created_at
          FROM quant.personal_trade_plans
         WHERE created_at<=%s AND valid_until>=%s ORDER BY symbol,created_at DESC""", (now, now))
    return [{**{k: v for k, v in row.items() if k not in {"created_at", "stop_price", "max_position_pct"}},
             "stop_price": _f(row["stop_price"]), "max_position_pct": _f(row["max_position_pct"], 1)} for row in rows]


def candidates(connection: Any, now: datetime) -> list[list[Any]]:
    run = connection.execute(
        """SELECT run_id FROM quant.post_close_strategy_candidates WHERE discovered_at<=%s
            ORDER BY discovered_at DESC LIMIT 1""", (now,),
    ).fetchone()
    if run is None:
        return []
    rows = _rows(connection, """
        SELECT rank,symbol,candidate_type,score,reason_codes,structure->'metrics' AS metrics,discovered_at
          FROM quant.post_close_strategy_candidates WHERE run_id=%s ORDER BY rank LIMIT 12""", (run["run_id"],))
    return [[r["rank"], r["symbol"], r["candidate_type"], _f(r["score"], 1), r["discovered_at"].astimezone(SHANGHAI).strftime("%m-%d"),
             {k: (r["metrics"] or {}).get(k) for k in ("support_price", "resistance_price", "close_to_resistance_pct", "sma20")}]
            for r in rows]


def recommendation_pool(connection: Any, now: datetime) -> dict[str, Any] | None:
    """The platform's latest reviewed recommendation/observation pool available at ``now``."""
    row = connection.execute(
        """SELECT as_of_date,created_at,result FROM quant.recommendation_pool_decisions
            WHERE created_at<=%s ORDER BY created_at DESC LIMIT 1""", (now,),
    ).fetchone()
    if row is None:
        return None
    result = row["result"] or {}
    keep = ("symbol", "name", "stage", "sector", "priority", "trigger", "why_now", "invalidation", "decision")
    return {
        "as_of_date": row["as_of_date"].isoformat(), "valid_until": result.get("valid_until"),
        "market_assessment": result.get("market_assessment"),
        "recommended": [{k: item.get(k) for k in keep} for item in result.get("recommended") or []],
        "reviewed_observe": [{k: item.get(k) for k in ("symbol", "name", "decision", "invalidation", "why_now")}
                             for item in result.get("reviewed") or [] if item.get("decision") != "recommend"][:12],
        "strategy_screening_symbols": [[item.get("symbol"), item.get("name"),
                                        [m.get("lane") for m in item.get("memberships") or []]]
                                       for item in result.get("screening") or []][:40],
    }


def intraday_strategy_scan(connection: Any, now: datetime, day: date) -> dict[str, Any] | None:
    """Today's nine-lane intraday scan, only when one was run today before ``now``."""
    row = connection.execute(
        """SELECT cutoff,result FROM quant.intraday_strategy_scans
            WHERE state='completed' AND cutoff<=%s AND cutoff>=%s ORDER BY cutoff DESC LIMIT 1""",
        (now, datetime.combine(day, datetime.min.time(), SHANGHAI)),
    ).fetchone()
    if row is None:
        return None
    lanes = []
    for lane in (row["result"] or {}).get("lanes") or []:
        items = lane.get("top") or lane.get("items") or []
        lanes.append({"lane": lane.get("key"), "label": lane.get("label"),
                      "top": [[i.get("symbol"), i.get("name"), i.get("state"), i.get("price"), i.get("reason")] for i in items[:5]]})
    return {"cutoff": row["cutoff"].astimezone(SHANGHAI).strftime("%H:%M"), "lanes": lanes}


def watchlist(connection: Any) -> list[list[Any]]:
    return [[r["symbol"], r["label"]] for r in _rows(connection, """
        SELECT symbol,label FROM quant.intraday_watchlists WHERE enabled ORDER BY symbol""", ())]


async def build_context(connection_factory: Callable[[], Any], *, now: datetime, account: dict[str, Any],
                        positions: list[dict[str, Any]], open_orders: list[dict[str, Any]],
                        today_orders: list[dict[str, Any]], recent_decisions: list[dict[str, Any]],
                        fetch_quotes: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]] = fetch_live_quotes,
                        fetch_indices: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]] = fetch_index_quotes,
                        fetch_minute: Callable[[list[str], date], Awaitable[dict[str, dict[str, Any]]]] = fetch_minutes,
                        ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return the prompt context and the raw live quotes used to build it."""
    day = now.astimezone(SHANGHAI).date()
    with connection_factory() as connection:
        watch = watchlist(connection)
        plan_rows = plans(connection, now)
        candidate_rows = candidates(connection, now)
        pool = recommendation_pool(connection, now)
        scan = intraday_strategy_scan(connection, now, day)
        db_part = {
            "board_flow": board_flow(connection, now, day), "market_breadth": breadth(connection, now, day),
            "board_rotations": rotations(connection, now, day), "limit_up_events": limit_events(connection, now, day),
            "intraday_signals": signals(connection, now, day), "news_research": news(connection, now),
        }
    held = [row["symbol"] for row in positions if int(row["quantity"]) > 0]
    focus = [s for s in (account.get("memory") or {}).get("focus_symbols") or [] if isinstance(s, str)]
    detail = list(dict.fromkeys(held + [o["symbol"] for o in open_orders] + focus + [p["symbol"] for p in plan_rows]))[:DETAIL_LIMIT]
    pool_symbols = [item["symbol"] for item in (pool or {}).get("recommended", []) + (pool or {}).get("reviewed_observe", [])]
    universe = list(dict.fromkeys(detail + pool_symbols + [row[0] for row in watch] + [row[1] for row in candidate_rows]))
    quotes, indices = await asyncio.gather(fetch_quotes(universe), fetch_indices(list(INDEX_SYMBOLS)))
    minutes = await fetch_minute(detail, day)
    with connection_factory() as connection:
        daily = daily_bars(connection, detail, day)
    cash = dec(account["cash"])
    market_value = sum((dec((quotes.get(row["symbol"]) or {}).get("price") or row["average_cost"]) * int(row["quantity"])
                        for row in positions), Decimal("0"))
    equity = cash + market_value
    context = {
        "now": now.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
        "account": {
            "cash": float(cash), "equity_live": round(float(equity), 2), "initial_equity": float(dec(account["initial_equity"])),
            "start_date": str(account["start_date"]),
            "positions": [{
                "symbol": row["symbol"], "name": row.get("name") or (quotes.get(row["symbol"]) or {}).get("name"),
                "quantity": int(row["quantity"]), "sellable": int(row["sellable_quantity"]),
                "avg_cost": _f(row["average_cost"], 3), "price": (quotes.get(row["symbol"]) or {}).get("price"),
                "weight_pct": round(float(dec((quotes.get(row["symbol"]) or {}).get("price") or row["average_cost"]) * int(row["quantity"]) / equity * 100), 1) if equity > 0 else None,
            } for row in positions if int(row["quantity"]) > 0],
            "open_orders": [{"order_id": str(o["order_id"]), "symbol": o["symbol"], "side": o["side"], "quantity": o["quantity"],
                             "limit_price": _f(o["limit_price"]), "placed_at": o["placed_at"].astimezone(SHANGHAI).strftime("%H:%M")}
                            for o in open_orders],
            "today_orders": [[o["placed_at"].astimezone(SHANGHAI).strftime("%H:%M:%S"), o["symbol"], o["side"], o["order_type"],
                              o["quantity"], o["status"], o["filled_quantity"], _f(o["fill_price"], 3), o.get("reject_reasons")]
                             for o in today_orders],
        },
        "indices": {INDEX_SYMBOLS[s]: v for s, v in indices.items()},
        **db_part,
        "detail_symbols": {symbol: {"quote": compact_quote(quotes[symbol]) if symbol in quotes else None,
                                    "minutes_today": minutes.get(symbol), "daily_last_20_mmdd_o_h_l_c_pct_amount_yi": daily.get(symbol)}
                           for symbol in detail},
        "watchlist_quotes_symbol_name_price_pct": [[s, (quotes.get(s) or {}).get("name") or label,
                                                    (quotes.get(s) or {}).get("price"),
                                                    compact_quote(quotes[s])["pct"] if s in quotes else None]
                                                   for s, label in watch],
        "human_active_plans": plan_rows,
        "platform_recommendation_pool": pool,
        "platform_intraday_strategy_scan_today": scan,
        "post_close_candidates_rank_symbol_type_score_date_metrics": candidate_rows,
        "your_recent_decisions": recent_decisions,
        "your_memory": (account.get("memory") or {}).get("notes"),
    }
    return context, quotes


__all__ = ["INDEX_SYMBOLS", "build_context", "compact_quote", "fetch_index_quotes", "fetch_live_quotes", "fetch_minutes",
           "summarize_minutes"]
