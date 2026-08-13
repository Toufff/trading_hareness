"""Native async read projections for research-readiness control-plane data."""

from __future__ import annotations

from typing import Any


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
