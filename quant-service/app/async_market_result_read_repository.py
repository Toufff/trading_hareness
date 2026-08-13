"""Native async projections for bounded market-result reads."""

from __future__ import annotations

from typing import Any, Iterable

from fastapi import HTTPException


def _limit(value: int, maximum: int) -> int:
    return max(1, min(value, maximum))


async def tushare_raw(async_database: Any, api_name: str, provider: str | None, limit: int, offset: int, catalog: Iterable[str]) -> dict[str, Any]:
    if api_name not in catalog:
        raise HTTPException(status_code=404, detail="api_name is not in the enabled catalog")
    limit, offset = _limit(limit, 500), max(0, offset)
    condition, values = ("api_name=%s", [api_name]) if provider is None else ("api_name=%s AND provider_key=%s", [api_name, provider])
    async with async_database.transaction() as conn:
        result = await conn.execute(f"SELECT provider_key,api_name,request_key,record_index,record_key,row_data,available_at,created_at FROM quant.tushare_raw_records WHERE {condition} ORDER BY available_at DESC,record_index LIMIT %s OFFSET %s", (*values, limit, offset))
        rows = await result.fetchall()
        total_result = await conn.execute(f"SELECT count(*)::int total FROM quant.tushare_raw_records WHERE {condition}", values)
        total = (await total_result.fetchone())["total"]
    return {"api_name": api_name, "provider": provider, "items": rows, "limit": limit, "offset": offset, "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


async def market_snapshots(async_database: Any, limit: int) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute("""SELECT session,exchange_date,observed_at,universe_key,universe_count,quote_count,coverage,status,decision_eligible,source_summary,summary,quality_flags,created_at,updated_at FROM quant.market_snapshot_runs ORDER BY exchange_date DESC,observed_at DESC LIMIT %s""", (_limit(limit, 100),))
        rows = await result.fetchall()
    return {"items": rows}


async def offline_minute_imports(async_database: Any, limit: int, offline_directory: str) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute("SELECT import_id,source_name,file_name,dataset_kind,status,row_count,rejected_rows,error_message,started_at,finished_at FROM quant.offline_imports ORDER BY started_at DESC LIMIT %s", (_limit(limit, 100),))
        rows = await result.fetchall()
    return {"items": rows, "offline_directory": offline_directory}


async def _current_data_coverage(conn: Any) -> dict[str, Any]:
    result = await conn.execute(
        """WITH daily_counts AS (
             SELECT trading_date,count(DISTINCT symbol)::int symbols
               FROM quant.canonical_bars_daily WHERE symbol<>'000300.SH' GROUP BY trading_date
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
                  (SELECT count(DISTINCT symbol)::int FROM quant.market_bars_minute) minute_symbols""")
    return dict(await result.fetchone() or {})


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def _feature_readiness(conn: Any) -> dict[str, Any]:
    result = await conn.execute(
        """WITH universe AS (
             SELECT count(*)::int symbols FROM quant.universe_members WHERE universe_key='all_a' AND enabled
           )
           SELECT 'daily_bars' feature,count(DISTINCT symbol)::int symbols,count(*)::int rows,max(trading_date) latest_date,'P0' priority
             FROM quant.canonical_bars_daily WHERE symbol<>'000300.SH'
           UNION ALL SELECT 'daily_basic',count(DISTINCT symbol)::int,count(*)::int,max(trading_date),'P0' FROM quant.daily_fundamentals
           UNION ALL SELECT 'trade_limits',count(DISTINCT symbol)::int,count(*)::int,max(trading_date),'P0' FROM quant.daily_trade_limits
           UNION ALL SELECT 'moneyflow_dc',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P0'
             FROM quant.tushare_raw_records WHERE api_name='moneyflow_dc'
           UNION ALL SELECT 'moneyflow',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P1'
             FROM quant.tushare_raw_records WHERE api_name='moneyflow'
           UNION ALL SELECT 'cyq_perf',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P1'
             FROM quant.tushare_raw_records WHERE api_name='cyq_perf'
           UNION ALL SELECT 'cyq_chips',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P1'
             FROM quant.tushare_raw_records WHERE api_name='cyq_chips'
           UNION ALL SELECT 'stk_factor_pro',count(DISTINCT row_data->>'ts_code')::int,count(*)::int,max(to_date(NULLIF(row_data->>'trade_date',''),'YYYYMMDD')),'P1'
             FROM quant.tushare_raw_records WHERE api_name='stk_factor_pro'
           UNION ALL SELECT 'sector_flow',count(DISTINCT sector_key)::int,count(*)::int,max(trading_date),'P1' FROM quant.sector_market_observations
           UNION ALL SELECT 'announcements',count(DISTINCT symbol)::int,count(*)::int,max(occurred_at::date),'P1' FROM quant.market_events
           UNION ALL SELECT 'analyst_claims',count(DISTINCT subject_key)::int,count(*)::int,max(available_at::date),'P1' FROM quant.analyst_claims""")
    rows = await result.fetchall()
    result = await conn.execute("SELECT greatest(1,count(*)::int) symbols FROM quant.universe_members WHERE universe_key='all_a' AND enabled")
    universe_size = int((await result.fetchone())["symbols"])
    items: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        coverage = min(1.0, _number(row["symbols"]) / max(1, universe_size)) if row["feature"] not in {"sector_flow", "analyst_claims"} else None
        coverage_ready = _number(row["symbols"]) >= min(universe_size * 0.8, 1000)
        status = "ready" if row["feature"] in {"daily_bars", "daily_basic", "trade_limits"} and coverage_ready else "partial"
        if row["feature"] != "daily_bars" and int(row["rows"] or 0) == 0:
            status = "missing"
        items.append({**row, "coverage": coverage, "status": status})
    blockers = [item["feature"] for item in items if item["feature"] in {"daily_bars", "daily_basic", "trade_limits"} and item["status"] != "ready"]
    return {"universe_key": "all_a", "universe_symbols": universe_size, "items": items,
            "decision_ready": not blockers, "blockers": blockers}


async def research_overview(async_database: Any, history_estimate: dict[str, Any]) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute(
            """SELECT (SELECT count(*)::int FROM quant.remote_reports) remote_reports,
                      (SELECT count(*)::int FROM quant.analyst_claims) claims,
                      (SELECT count(*)::int FROM quant.canonical_bars_daily) canonical_bars,
                      (SELECT count(*)::int FROM quant.tushare_raw_records) tushare_raw_records,
                      (SELECT count(*)::int FROM quant.market_trade_calendar) calendar_days,
                      (SELECT count(*)::int FROM quant.daily_fundamentals) fundamentals,
                      (SELECT count(*)::int FROM quant.daily_trade_limits) trade_limits,
                      (SELECT count(*)::int FROM quant.market_bars_minute) offline_minute_bars,
                      (SELECT count(*)::int FROM quant.market_snapshot_runs) market_snapshot_runs,
                      (SELECT count(*)::int FROM quant.market_events) market_events,
                      (SELECT count(*)::int FROM quant.universe_members WHERE universe_key='all_a' AND enabled) all_a_symbols,
                      (SELECT count(*)::int FROM quant.sectors) sectors,
                      (SELECT count(*)::int FROM quant.sector_membership_history WHERE effective_to IS NULL) active_sector_memberships,
                      (SELECT count(*)::int FROM quant.sector_market_observations) sector_market_observations,
                      (SELECT count(*)::int FROM quant.offline_imports WHERE status IN ('completed','partial')) offline_imports,
                      (SELECT count(*)::int FROM quant.fetch_runs WHERE status='running') running_fetch_runs,
                      (SELECT count(*)::int FROM quant.fetch_runs WHERE status='running' AND coalesce(started_at,created_at)<now()-interval '90 minutes') stale_fetch_runs,
                      (SELECT count(*)::int FROM quant.data_quality_issues WHERE resolved_at IS NULL) quality_issues""")
        counts = await result.fetchone()
        result = await conn.execute("SELECT snapshot_key,as_of_date,knowledge_cutoff,status,manifest,finalized_at FROM quant.data_snapshots ORDER BY created_at DESC LIMIT 1")
        last_snapshot = await result.fetchone()
        result = await conn.execute("SELECT run_id,as_of_date,model_version,market_regime,source_status,created_at FROM quant.recommendation_runs ORDER BY created_at DESC LIMIT 1")
        latest_run = await result.fetchone()
        result = await conn.execute("""SELECT session,exchange_date,observed_at,universe_key,universe_count,quote_count,coverage,status,decision_eligible,source_summary,summary,quality_flags,updated_at FROM quant.market_snapshot_runs ORDER BY exchange_date DESC,observed_at DESC LIMIT 1""")
        latest_market_snapshot = await result.fetchone()
        coverage = await _current_data_coverage(conn)
        readiness = await _feature_readiness(conn)
    return {"counts": counts, "latest_snapshot": last_snapshot, "latest_market_snapshot": latest_market_snapshot,
            "latest_recommendation_run": latest_run, "data_coverage": coverage, "history_estimate": history_estimate,
            "feature_readiness": readiness}


async def analyst_scorecards(async_database: Any, limit: int) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute("SELECT analyst_id,horizon_days,as_of_date,observations,hit_rate,mean_excess_return,mean_directional_return,calibration_score,methodology_version,created_at FROM quant.analyst_scorecards ORDER BY as_of_date DESC,observations DESC,analyst_id,horizon_days LIMIT %s", (_limit(limit, 500),))
        rows = await result.fetchall()
        result = await conn.execute(
            """SELECT a.remote_analyst_id,a.name,count(DISTINCT c.claim_id)::int stock_claims,
                      count(DISTINCT c.claim_id) FILTER (WHERE c.direction<>0)::int directional_stock_claims,
                      count(DISTINCT c.claim_id) FILTER (WHERE c.direction=0)::int neutral_stock_claims,
                      count(DISTINCT o.outcome_id)::int settled_stock_outcomes,max(c.available_at) latest_claim_at
                 FROM quant.remote_analysts a
                 LEFT JOIN quant.analyst_claims c ON c.remote_analyst_id=a.remote_analyst_id AND c.scope='stock'
                 LEFT JOIN quant.outcomes o ON o.claim_id=c.claim_id
                GROUP BY a.remote_analyst_id,a.name ORDER BY a.name,a.remote_analyst_id""")
        readiness_rows = await result.fetchall()
    readiness = []
    for row in readiness_rows:
        item = dict(row)
        directional, settled = int(item["directional_stock_claims"] or 0), int(item["settled_stock_outcomes"] or 0)
        reason = "no_directional_stock_claims" if directional == 0 else ("fewer_than_30_settled_stock_outcomes" if settled < 30 else "eligible_for_scorecard_review")
        readiness.append({**item, "mature": settled >= 30, "reason": reason})
    return {"items": rows, "readiness": readiness, "notice": "成绩单只对方向明确、且后续价格路径已经成熟的股票观点计算。"}


async def latest_recommendations(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as conn:
        result = await conn.execute("SELECT * FROM quant.recommendation_runs ORDER BY created_at DESC LIMIT 1")
        run = await result.fetchone()
        if not run:
            return {"run": None, "recommendations": []}
        result = await conn.execute("SELECT * FROM quant.recommendations WHERE run_id=%s ORDER BY rank LIMIT 500", (run["run_id"],))
        rows = await result.fetchall()
    return {"run": run, "recommendations": rows}


async def metrics(async_database: Any) -> Any:
    async with async_database.transaction() as conn:
        result = await conn.execute("SELECT (SELECT count(*) FROM quant.market_bars_daily) bars,(SELECT count(*) FROM quant.analyst_signals) signals,(SELECT count(*) FROM quant.remote_reports) remote_reports,(SELECT count(*) FROM quant.analyst_claims) analyst_claims,(SELECT count(*) FROM quant.tushare_raw_records) tushare_raw_records,(SELECT count(*) FROM quant.market_bars_minute) offline_minute_bars,(SELECT count(*) FROM quant.recommendation_runs) recommendation_runs")
        return await result.fetchone()
