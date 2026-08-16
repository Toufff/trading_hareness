"""Native async read projections for research-readiness control-plane data."""

from __future__ import annotations

from typing import Any

from .replay_readiness import replay_readiness_payload
from .research_capacity import historical_capacity_plan


async def frameworks(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            "SELECT framework_key,label,role,integration_mode,status,license_note,prerequisites,metadata,updated_at FROM quant.research_frameworks ORDER BY framework_key"
        )
        rows = await result.fetchall()
    return {"items": rows}


async def feature_readiness(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        # Keep the existing readiness function's SQL and output contract in
        # the synchronous compatibility path; this projection mirrors its
        # query so dashboard reads never borrow that connection.
        result = await connection.execute(
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
               UNION ALL SELECT 'analyst_claims',count(DISTINCT subject_key)::int,count(*)::int,max(available_at::date),'P1' FROM quant.analyst_claims"""
        )
        rows = await result.fetchall()
        result = await connection.execute("SELECT greatest(1,count(*)::int) symbols FROM quant.universe_members WHERE universe_key='all_a' AND enabled")
        universe_size = int((await result.fetchone())["symbols"])
    items = []
    for row in rows:
        row = dict(row)
        coverage = min(1.0, float(row["symbols"] or 0) / max(1, universe_size)) if row["feature"] not in {"sector_flow", "analyst_claims"} else None
        ready = row["feature"] in {"daily_bars", "daily_basic", "trade_limits"} and coverage >= 0.8
        status = "ready" if ready else "partial"
        if row["feature"] not in {"daily_bars"} and int(row["rows"] or 0) == 0:
            status = "missing"
        items.append({**row, "coverage": coverage, "status": status})
    blockers = [row["feature"] for row in items if row["status"] != "ready"]
    return {"universe_key": "all_a", "universe_symbols": universe_size, "items": items,
            "decision_ready": not blockers, "blockers": blockers}


async def replay_readiness(async_database: Any) -> dict[str, Any]:
    """Read bounded replay gates using the native async connection."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """WITH universe AS (
                    SELECT greatest(1,count(*)::int) symbols
                      FROM quant.universe_members
                     WHERE universe_key='all_a' AND enabled
                 ), daily_counts AS (
                    SELECT trading_date,count(DISTINCT symbol)::int symbols
                      FROM quant.canonical_bars_daily
                     WHERE symbol<>'000300.SH'
                     GROUP BY trading_date
                 )
                SELECT
                  (SELECT min(trading_date) FROM daily_counts) first_daily_date,
                  (SELECT max(trading_date) FROM daily_counts) latest_daily_date,
                  (SELECT count(*)::int FROM daily_counts d,universe u
                    WHERE d.symbols>=least(u.symbols*0.8,1000)) full_cross_section_days,
                  (SELECT count(DISTINCT (bar_time AT TIME ZONE 'Asia/Shanghai')::date)::int
                     FROM quant.market_bars_minute) offline_minute_trading_days,
                  (SELECT count(DISTINCT symbol)::int FROM quant.market_bars_minute) offline_minute_symbols,
                  (SELECT count(*)::int FROM quant.market_bars_minute) offline_minute_bars,
                  (SELECT count(*)::int FROM quant.market_bars_minute WHERE source_available_at IS NOT NULL) offline_minute_source_clock_bars,
                  (SELECT count(DISTINCT (source_available_at AT TIME ZONE 'Asia/Shanghai')::date)::int
                     FROM quant.market_bars_minute WHERE source_available_at IS NOT NULL) offline_minute_source_clock_days,
                  (SELECT count(*)::int FROM quant.offline_imports WHERE status IN ('completed','partial')) completed_offline_imports,
                  (SELECT count(*)::int FROM quant.intraday_signal_events
                    WHERE state IN ('confirmed','alerted')) confirmed_signal_events,
                  (SELECT count(DISTINCT signal_event_id)::int FROM quant.intraday_signal_outcomes
                    WHERE status='matured') matured_signal_events"""
        )
        row = await result.fetchone()
    return replay_readiness_payload(dict(row or {}))


async def historical_estimate(async_database: Any, request: Any) -> dict[str, Any]:
    """Estimate local research capacity without a synchronous DB handoff."""
    async with async_database.transaction() as connection:
        if request.universe_symbols:
            universe_symbols = int(request.universe_symbols)
        else:
            result = await connection.execute(
                """SELECT coalesce(nullif((SELECT count(*)::int FROM quant.universe_members
                                             WHERE universe_key='all_a' AND enabled),0),
                              nullif((SELECT count(*)::int FROM quant.instruments
                                       WHERE symbol ~ '^\\d{6}\\.(SH|SZ|BJ)$'),0),5500) symbols"""
            )
            universe_symbols = int((await result.fetchone())["symbols"])
        result = await connection.execute(
            """SELECT api_name,ceil(avg(pg_column_size(row_data)))::int avg_bytes
                 FROM quant.tushare_raw_records
                WHERE api_name = ANY(%s) GROUP BY api_name""",
            (["daily", "adj_factor", "daily_basic", "stk_limit", "moneyflow_dc", "moneyflow",
              "cyq_perf", "cyq_chips", "stk_factor_pro", "suspend_d"],),
        )
        samples = await result.fetchall()
        result = await connection.execute("SELECT count(*)::int total FROM quant.sectors")
        sector_count = int((await result.fetchone())["total"])
        coverage_result = await connection.execute(
            """WITH daily_counts AS (
                 SELECT trading_date,count(DISTINCT symbol)::int symbols
                   FROM quant.canonical_bars_daily WHERE symbol<>'000300.SH' GROUP BY trading_date
               ), universe AS (
                 SELECT greatest(1,(SELECT count(*)::int FROM quant.universe_members
                                    WHERE universe_key='all_a' AND enabled)) AS symbols
               )
               SELECT (SELECT min(trading_date) FROM quant.canonical_bars_daily) first_bar_date,
                      (SELECT max(trading_date) FROM quant.canonical_bars_daily) latest_bar_date,
                      (SELECT count(*)::int FROM daily_counts) bar_days,
                      (SELECT count(*)::int FROM daily_counts,universe
                        WHERE daily_counts.symbols>=least(universe.symbols*0.8,1000)) full_cross_section_days,
                      (SELECT max(symbols) FROM daily_counts) max_symbols_on_day,
                      (SELECT count(DISTINCT symbol)::int FROM quant.daily_fundamentals) fundamental_symbols,
                      (SELECT count(DISTINCT symbol)::int FROM quant.daily_trade_limits) limit_symbols,
                      (SELECT count(DISTINCT symbol)::int FROM quant.market_bars_minute) minute_symbols"""
        )
        coverage = dict(await coverage_result.fetchone() or {})
    return {
        **historical_capacity_plan(
            request.years, universe_symbols, request.trading_days_per_year, request.include_minute,
            {row["api_name"]: row["avg_bytes"] for row in samples}, sector_count,
        ),
        "current_coverage": coverage,
        "assumptions": {
            "storage_multiplier": 1.35,
            "row_size_source": "current raw samples when present, otherwise conservative constants",
            "minute_policy": "not included unless include_minute=true; historical minute remains offline-file only",
        },
    }
