"""Pure capacity estimates and local research readiness projections.

This module deliberately has no provider or HTTP dependency.  It is shared by
the compatibility exports and the read-only control-plane routes; callers own
the database transaction boundary.
"""

from __future__ import annotations

from typing import Any


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def bytes_to_gib(value: int | float) -> float:
    return round(float(value) / (1024 ** 3), 3)


def historical_capacity_plan(years: int, universe_symbols: int, trading_days_per_year: int = 244,
                             include_minute: bool = False, raw_row_bytes: dict[str, int] | None = None,
                             sector_count: int = 0) -> dict[str, Any]:
    """Estimate storage/request size; never calls providers or mutates data."""
    raw_row_bytes = raw_row_bytes or {}
    trading_days, symbols = years * trading_days_per_year, max(1, universe_symbols)

    def row_bytes(api_name: str, fallback: int) -> int:
        return max(80, int(raw_row_bytes.get(api_name) or fallback))

    datasets = [
        {"dataset": "daily", "label": "A股日线 OHLCV", "rows": symbols * trading_days, "bytes_per_row": row_bytes("daily", 320), "priority": "P0", "policy": "online_window_or_offline_bulk"},
        {"dataset": "adj_factor", "label": "复权因子", "rows": symbols * trading_days, "bytes_per_row": row_bytes("adj_factor", 180), "priority": "P0", "policy": "online_window_or_offline_bulk"},
        {"dataset": "daily_basic", "label": "每日估值与换手", "rows": symbols * trading_days, "bytes_per_row": row_bytes("daily_basic", 520), "priority": "P0", "policy": "online_window_or_offline_bulk"},
        {"dataset": "stk_limit", "label": "涨跌停价格", "rows": symbols * trading_days, "bytes_per_row": row_bytes("stk_limit", 180), "priority": "P0", "policy": "online_window_or_offline_bulk"},
        {"dataset": "moneyflow_dc", "label": "东财主力/散户资金流", "rows": symbols * trading_days, "bytes_per_row": row_bytes("moneyflow_dc", 720), "priority": "P0", "policy": "specialty_source_throttled"},
        {"dataset": "moneyflow", "label": "Tushare个股资金流", "rows": symbols * trading_days, "bytes_per_row": row_bytes("moneyflow", 620), "priority": "P1", "policy": "specialty_source_throttled"},
        {"dataset": "cyq_perf", "label": "筹码胜率摘要", "rows": symbols * trading_days, "bytes_per_row": row_bytes("cyq_perf", 360), "priority": "P1", "policy": "specialty_source_throttled"},
        {"dataset": "cyq_chips", "label": "筹码分布明细", "rows": symbols * trading_days * 8, "bytes_per_row": row_bytes("cyq_chips", 260), "priority": "P1", "policy": "specialty_source_throttled"},
        {"dataset": "stk_factor_pro", "label": "专业技术因子", "rows": symbols * trading_days, "bytes_per_row": row_bytes("stk_factor_pro", 1100), "priority": "P1", "policy": "specialty_source_throttled"},
        {"dataset": "suspend_d", "label": "停复牌事件", "rows": max(symbols, int(symbols * trading_days * 0.01)), "bytes_per_row": row_bytes("suspend_d", 260), "priority": "P1", "policy": "sparse_event"},
        {"dataset": "sector_flow", "label": "行业/概念资金流", "rows": max(1, sector_count or 2000) * trading_days, "bytes_per_row": 520, "priority": "P1", "policy": "board_daily"},
    ]
    if include_minute:
        datasets.append({"dataset": "minute_1m", "label": "1分钟线", "rows": symbols * trading_days * 240,
                         "bytes_per_row": 180, "priority": "offline_only", "policy": "mounted_csv_or_parquet_only"})
    total_bytes, normalized = 0, []
    for item in datasets:
        payload_bytes = int(item["rows"] * item["bytes_per_row"])
        storage_bytes = int(payload_bytes * 1.35)
        total_bytes += storage_bytes
        normalized.append({**item, "payload_gib": bytes_to_gib(payload_bytes), "estimated_storage_gib": bytes_to_gib(storage_bytes)})
    return {"years": years, "trading_days": trading_days, "universe_symbols": symbols,
            "include_minute": include_minute, "estimated_storage_gib": bytes_to_gib(total_bytes),
            "datasets": normalized,
            "policy": "容量估算不触发历史下载；3-5年批量历史优先使用离线文件或分批窗口任务。"}


def current_data_coverage(connection: Any) -> dict[str, Any]:
    row = connection.execute(
        """WITH daily_counts AS (
             SELECT trading_date,count(DISTINCT symbol)::int symbols FROM quant.canonical_bars_daily
              WHERE symbol<>'000300.SH' GROUP BY trading_date
           ), universe AS (
             SELECT greatest(1,(SELECT count(*)::int FROM quant.universe_members WHERE universe_key='all_a' AND enabled)) AS symbols
           )
           SELECT (SELECT min(trading_date) FROM quant.canonical_bars_daily) first_bar_date,
                  (SELECT max(trading_date) FROM quant.canonical_bars_daily) latest_bar_date,
                  (SELECT count(*)::int FROM daily_counts) bar_days,
                  (SELECT count(*)::int FROM daily_counts,universe WHERE daily_counts.symbols>=least(universe.symbols*0.8,1000)) full_cross_section_days,
                  (SELECT max(symbols) FROM daily_counts) max_symbols_on_day,
                  (SELECT count(DISTINCT symbol)::int FROM quant.daily_fundamentals) fundamental_symbols,
                  (SELECT count(DISTINCT symbol)::int FROM quant.daily_trade_limits) limit_symbols,
                  (SELECT count(DISTINCT symbol)::int FROM quant.market_bars_minute) minute_symbols""").fetchone()
    return dict(row or {})


def feature_readiness_state(connection: Any) -> dict[str, Any]:
    rows = connection.execute(
        """SELECT 'daily_bars' feature,count(DISTINCT symbol)::int symbols,count(*)::int rows,max(trading_date) latest_date,'P0' priority FROM quant.canonical_bars_daily WHERE symbol<>'000300.SH'
           UNION ALL SELECT 'daily_basic',count(DISTINCT symbol)::int,count(*)::int,max(trading_date),'P0' FROM quant.daily_fundamentals
           UNION ALL SELECT 'trade_limits',count(DISTINCT symbol)::int,count(*)::int,max(trading_date),'P0' FROM quant.daily_trade_limits
           UNION ALL SELECT 'moneyflow_dc',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P0' FROM quant.tushare_raw_records WHERE api_name='moneyflow_dc'
           UNION ALL SELECT 'moneyflow',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P1' FROM quant.tushare_raw_records WHERE api_name='moneyflow'
           UNION ALL SELECT 'cyq_perf',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P1' FROM quant.tushare_raw_records WHERE api_name='cyq_perf'
           UNION ALL SELECT 'cyq_chips',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P1' FROM quant.tushare_raw_records WHERE api_name='cyq_chips'
           UNION ALL SELECT 'stk_factor_pro',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P1' FROM quant.tushare_raw_records WHERE api_name='stk_factor_pro'
           UNION ALL SELECT 'sector_flow',count(DISTINCT sector_key)::int,count(*)::int,max(trading_date),'P1' FROM quant.sector_market_observations
           UNION ALL SELECT 'announcements',count(DISTINCT symbol)::int,count(*)::int,max(occurred_at::date),'P1' FROM quant.market_events
           UNION ALL SELECT 'analyst_claims',count(DISTINCT subject_key)::int,count(*)::int,
              max((available_at AT TIME ZONE 'Asia/Shanghai')::date),'P1' FROM quant.analyst_claims""").fetchall()
    universe_size = connection.execute("SELECT greatest(1,count(*)::int) symbols FROM quant.universe_members WHERE universe_key='all_a' AND enabled").fetchone()["symbols"]
    items = []
    for row in rows:
        row = dict(row)
        coverage = min(1.0, number(row["symbols"]) / max(1, int(universe_size))) if row["feature"] not in {"sector_flow", "analyst_claims"} else None
        coverage_ready = number(row["symbols"]) >= min(int(universe_size) * 0.8, 1000)
        status = "ready" if row["feature"] in {"daily_bars", "daily_basic", "trade_limits"} and coverage_ready else "partial"
        if row["feature"] != "daily_bars" and int(row["rows"] or 0) == 0: status = "missing"
        items.append({**row, "coverage": coverage, "status": status})
    blockers = [item["feature"] for item in items if item["feature"] in {"daily_bars", "daily_basic", "trade_limits"} and item["status"] != "ready"]
    return {"universe_key": "all_a", "universe_symbols": universe_size, "items": items,
            "decision_ready": not blockers, "blockers": blockers}


def historical_estimate_from_db(database: Any, request: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        universe_symbols = request.universe_symbols or connection.execute(
            """SELECT coalesce(nullif((SELECT count(*)::int FROM quant.universe_members WHERE universe_key='all_a' AND enabled),0),
                              nullif((SELECT count(*)::int FROM quant.instruments WHERE symbol ~ '^\\d{6}\\.(SH|SZ|BJ)$'),0),5500) symbols""").fetchone()["symbols"]
        samples = connection.execute(
            """SELECT api_name,ceil(avg(pg_column_size(row_data)))::int avg_bytes FROM quant.tushare_raw_records
                WHERE api_name = ANY(%s) GROUP BY api_name""",
            (["daily", "adj_factor", "daily_basic", "stk_limit", "moneyflow_dc", "moneyflow", "cyq_perf", "cyq_chips", "stk_factor_pro", "suspend_d"],),
        ).fetchall()
        sector_count = connection.execute("SELECT count(*)::int total FROM quant.sectors").fetchone()["total"]
        coverage = current_data_coverage(connection)
    return {**historical_capacity_plan(request.years, int(universe_symbols), request.trading_days_per_year,
                                        request.include_minute, {row["api_name"]: row["avg_bytes"] for row in samples}, int(sector_count or 0)),
            "current_coverage": coverage,
            "assumptions": {"storage_multiplier": 1.35, "row_size_source": "current raw samples when present, otherwise conservative constants",
                            "minute_policy": "not included unless include_minute=true; historical minute remains offline-file only"}}
