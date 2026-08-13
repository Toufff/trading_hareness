"""Native async projections for bounded market-result reads."""

from __future__ import annotations

from typing import Any, Iterable, Callable

from fastapi import HTTPException


def _limit(value: int, maximum: int) -> int:
    return max(1, min(value, maximum))


async def tushare_raw(db: Any, api_name: str, provider: str | None, limit: int, offset: int, catalog: Iterable[str]) -> dict[str, Any]:
    if api_name not in catalog:
        raise HTTPException(status_code=404, detail="api_name is not in the enabled catalog")
    limit, offset = _limit(limit, 500), max(0, offset)
    condition, values = ("api_name=%s", [api_name]) if provider is None else ("api_name=%s AND provider_key=%s", [api_name, provider])
    async with db.transaction() as conn:
        result = await conn.execute(f"SELECT provider_key,api_name,request_key,record_index,record_key,row_data,available_at,created_at FROM quant.tushare_raw_records WHERE {condition} ORDER BY available_at DESC,record_index LIMIT %s OFFSET %s", (*values, limit, offset))
        rows = await result.fetchall()
        total_result = await conn.execute(f"SELECT count(*)::int total FROM quant.tushare_raw_records WHERE {condition}", values)
        total = (await total_result.fetchone())["total"]
    return {"api_name": api_name, "provider": provider, "items": rows, "limit": limit, "offset": offset, "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


async def market_snapshots(db: Any, limit: int) -> dict[str, Any]:
    async with db.transaction() as conn:
        result = await conn.execute("""SELECT session,exchange_date,observed_at,universe_key,universe_count,quote_count,coverage,status,decision_eligible,source_summary,summary,quality_flags,created_at,updated_at FROM quant.market_snapshot_runs ORDER BY exchange_date DESC,observed_at DESC LIMIT %s""", (_limit(limit, 100),))
        rows = await result.fetchall()
    return {"items": rows}


async def offline_minute_imports(db: Any, limit: int, offline_directory: str) -> dict[str, Any]:
    async with db.transaction() as conn:
        result = await conn.execute("SELECT import_id,source_name,file_name,dataset_kind,status,row_count,rejected_rows,error_message,started_at,finished_at FROM quant.offline_imports ORDER BY started_at DESC LIMIT %s", (_limit(limit, 100),))
        rows = await result.fetchall()
    return {"items": rows, "offline_directory": offline_directory}


async def analyst_scorecards(db: Any, limit: int, readiness_fn: Callable[[Any], Any]) -> dict[str, Any]:
    async with db.transaction() as conn:
        result = await conn.execute("SELECT analyst_id,horizon_days,as_of_date,observations,hit_rate,mean_excess_return,mean_directional_return,calibration_score,methodology_version,created_at FROM quant.analyst_scorecards ORDER BY as_of_date DESC,observations DESC,analyst_id,horizon_days LIMIT %s", (_limit(limit, 500),))
        rows = await result.fetchall()
        # The readiness callback is a legacy sync query; route callers submit it
        # through the bounded DB executor instead of invoking it on this loop.
    return {"items": rows, "readiness": None, "notice": "成绩单只对方向明确、且后续价格路径已经成熟的股票观点计算。"}


async def latest_recommendations(db: Any) -> dict[str, Any]:
    async with db.transaction() as conn:
        result = await conn.execute("SELECT * FROM quant.recommendation_runs ORDER BY created_at DESC LIMIT 1")
        run = await result.fetchone()
        if not run:
            return {"run": None, "recommendations": []}
        result = await conn.execute("SELECT * FROM quant.recommendations WHERE run_id=%s ORDER BY rank LIMIT 500", (run["run_id"],))
        rows = await result.fetchall()
    return {"run": run, "recommendations": rows}


async def metrics(db: Any) -> Any:
    async with db.transaction() as conn:
        result = await conn.execute("SELECT (SELECT count(*) FROM quant.market_bars_daily) bars,(SELECT count(*) FROM quant.analyst_signals) signals,(SELECT count(*) FROM quant.remote_reports) remote_reports,(SELECT count(*) FROM quant.analyst_claims) analyst_claims,(SELECT count(*) FROM quant.tushare_raw_records) tushare_raw_records,(SELECT count(*) FROM quant.market_bars_minute) offline_minute_bars,(SELECT count(*) FROM quant.recommendation_runs) recommendation_runs")
        return await result.fetchone()

