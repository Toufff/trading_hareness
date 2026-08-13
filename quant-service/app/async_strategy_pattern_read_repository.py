"""Native async wrapper for persisted limit-pool pattern evidence."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

from .runtime_executors import run_database_blocking


async def latest_strategy_pattern_mining(
    async_database: Any,
    merge_limit_pool_sources_fn: Callable[..., dict[str, Any]],
    limit_board_count_fn: Callable[[Any], int],
    strategy_json_safe_fn: Callable[[Any], Any],
    post_close_limit_daily_features_fn: Callable[[list[dict[str, Any]]], dict[str, Any]],
    post_close_exact_board_context_fn: Callable[[Any], dict[str, Any]],
    post_close_tushare_lhb_context_fn: Callable[[Any], dict[str, Any]],
    database_runner: Callable[..., Any] = run_database_blocking,
) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute("SELECT run_id,run_key,as_of_date,model_version,status,source_status,summary,created_at,updated_at FROM quant.strategy_pattern_runs ORDER BY as_of_date DESC,updated_at DESC LIMIT 1")
        run = await result.fetchone()
        if not run:
            return {"run": None, "limit_pool": [], "limit_ladder": [], "pool_coverage": {}, "picks": [], "samples": [], "notice": "尚未运行涨停梯队与分钟拉升形态挖掘。"}
        result = await connection.execute("SELECT rank,symbol,name,primary_cohort,cohorts,board_context,limit_context,daily_features,intraday_pattern,minute_source,risk_flags FROM quant.strategy_pattern_samples WHERE run_id=%s ORDER BY rank", (run["run_id"],))
        rows = await result.fetchall()
        stamp = run["as_of_date"].strftime("%Y%m%d")
        result = await connection.execute("SELECT DISTINCT ON(row_data->>'ts_code') row_data,provider_key,available_at FROM quant.tushare_raw_records WHERE api_name='limit_list_ths' AND row_data->>'trade_date'=%s AND row_data->>'limit_type'='涨停池' ORDER BY row_data->>'ts_code',available_at DESC", (stamp,))
        pool_records = await result.fetchall()
        result = await connection.execute("SELECT DISTINCT ON(row_data->>'ts_code') row_data,provider_key,available_at FROM quant.tushare_raw_records WHERE api_name='limit_step' AND row_data->>'trade_date'=%s ORDER BY row_data->>'ts_code',available_at DESC", (stamp,))
        ladder_records = await result.fetchall()
        result = await connection.execute("SELECT DISTINCT ON(symbol) symbol,body,source,available_at FROM quant.market_events WHERE event_type='limit_up_pool' AND (occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s ORDER BY symbol,created_at DESC", (run["as_of_date"],))
        eastmoney_records = await result.fetchall()
        union = merge_limit_pool_sources_fn([dict(record) for record in pool_records], [dict(record) for record in eastmoney_records])
        pool = [{**item, "board_count": limit_board_count_fn(item.get("tag"))} for item in union["items"]]
        pool.sort(key=lambda item: (-int(item.get("board_count") or 0), -float(item.get("limit_amount") or 0), str(item.get("ts_code") or "")))
        symbols = [str(item.get("ts_code") or "") for item in pool]
        result = await connection.execute("""WITH ranked AS (SELECT b.*,row_number() OVER(PARTITION BY b.symbol ORDER BY b.trading_date DESC) rn FROM quant.canonical_bars_daily b WHERE b.symbol=ANY(%s) AND b.trading_date<=%s AND b.trading_date>=%s) SELECT * FROM ranked WHERE rn<=21 ORDER BY symbol,trading_date""", (symbols, run["as_of_date"], run["as_of_date"] - timedelta(days=60))) if symbols else None
        daily_records = await result.fetchall() if result else []
    daily_grouped: dict[str, list[dict[str, Any]]] = {}
    for record in daily_records:
        daily_grouped.setdefault(str(record["symbol"]), []).append(dict(record))
    board_contexts = await database_runner(post_close_exact_board_context_fn, run["as_of_date"], timeout_seconds=30)
    lhb_contexts = await database_runner(post_close_tushare_lhb_context_fn, run["as_of_date"], timeout_seconds=30)
    for item in pool:
        daily = post_close_limit_daily_features_fn(daily_grouped.get(str(item.get("ts_code") or ""), []))
        item.update({"daily_features": daily, "volume_multiple_5d": daily.get("volume_multiple_5d"), "volume_multiple_20d": daily.get("volume_multiple_20d"), "low_pct": daily.get("low_pct"), "board_context": board_contexts.get(str(item.get("ts_code") or "")), "lhb_context": lhb_contexts.get(str(item.get("ts_code") or ""))})
    pool_by_symbol = {str(item.get("ts_code") or ""): item for item in pool}
    limit_pool = [{**item, "rank": rank} for rank, item in enumerate(pool, start=1)]
    ladder_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol, context in pool_by_symbol.items():
        if int(context.get("board_count") or 0) >= 2:
            ladder_by_symbol[symbol] = {**context, "nums": int(context.get("board_count") or 0), "ladder_sources": ["tushare_limit_list_ths_tag"]}
    for record in ladder_records:
        item = strategy_json_safe_fn(dict(record["row_data"] or {})); symbol = str(item.get("ts_code") or ""); context = pool_by_symbol.get(symbol, {})
        ladder_by_symbol[symbol] = {**context, **item, "provider_key": record["provider_key"], "available_at": record["available_at"], "tag": context.get("tag") or item.get("tag"), "status": context.get("status"), "price": context.get("price"), "pct_chg": context.get("pct_chg"), "turnover_rate": context.get("turnover_rate"), "open_num": context.get("open_num"), "limit_amount": context.get("limit_amount"), "lu_desc": context.get("lu_desc"), "volume_multiple_5d": context.get("volume_multiple_5d"), "volume_multiple_20d": context.get("volume_multiple_20d"), "board_context": context.get("board_context"), "lhb_context": context.get("lhb_context"), "ladder_sources": list(dict.fromkeys([*(ladder_by_symbol.get(symbol, {}).get("ladder_sources") or []), "tushare_limit_step"]))}
    ladder = sorted(ladder_by_symbol.values(), key=lambda item: (-int(item.get("nums") or 0), -float(item.get("limit_amount") or 0), str(item.get("ts_code") or "")))
    limit_ladder = [{**item, "rank": rank} for rank, item in enumerate(ladder, start=1)]
    union["coverage"].update({"limit_step_count": len(ladder_records), "multi_board_union_count": len(limit_ladder)})
    samples = [dict(row) for row in rows]
    return {"run": run, "limit_pool": limit_pool, "limit_ladder": limit_ladder, "pool_coverage": union["coverage"], "picks": [item for item in samples if (item.get("limit_context") or {}).get("review_tier") != "research_sample"][:10], "samples": samples, "notice": "地天板、龙头、连板和首板均为盘后研究样本；实时阶段仍需点时量价、板块联动与承接确认。"}


__all__ = ["latest_strategy_pattern_mining"]
