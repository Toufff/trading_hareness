"""Dependency-injected active A-share universe synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json


async def sync(
    request: Any,
    *,
    provider_candidates: Callable[..., list[Any]],
    cn_date: Callable[[], Any],
    call_tushare_api: Callable[..., Awaitable[Any]],
    looks_like_response_header: Callable[[list[dict[str, Any]]], bool],
    persist_tushare_rows: Callable[..., int],
    run_database_blocking: Callable[..., Awaitable[Any]],
    persist_tushare_fetch_blocked: Callable[..., Any],
    db: Any,
    safe_error_detail: Callable[[str, int], str],
    provider_call_error: type[Exception],
    executor_saturated_error: type[Exception],
    record_provider_success: Callable[..., Any],
    record_provider_failure: Callable[..., Any],
    record_provider_api_capability: Callable[..., Any],
) -> dict[str, Any]:
    """Refresh ``all_a`` from one bounded stock_basic response."""
    candidates = provider_candidates("stock_basic", request.provider)
    if not candidates:
        return {"status": "blocked", "reason": "no configured provider supports stock_basic", "universe_key": request.universe_key}
    exchange_date = cn_date()
    params = {"exchange": "", "list_status": "L"}
    fields = "ts_code,symbol,name,area,industry,market,list_date,delist_date,exchange,is_hs"
    request_key = hashlib.sha256(json.dumps({"capability": "stock_basic_all_a", "date": str(exchange_date), "provider": request.provider}, sort_keys=True).encode()).hexdigest()

    def prepare_run() -> dict[str, Any] | None:
        with db.transaction() as connection:
            prior = connection.execute("SELECT status,row_count FROM quant.fetch_runs WHERE request_key=%s", (request_key,)).fetchone()
            if prior and prior["status"] == "completed":
                return {"status": "unchanged", "universe_key": request.universe_key, "imported": prior["row_count"], "request_key": request_key}
            connection.execute(
                """INSERT INTO quant.fetch_runs(provider_key,capability,trade_date,request_key,status,attempt_count,started_at,metadata)
                   VALUES(%s,'stock_basic_all_a',%s,%s,'running',1,now(),%s)
                   ON CONFLICT(request_key) DO UPDATE SET status='running',attempt_count=quant.fetch_runs.attempt_count+1,
                     started_at=now(),finished_at=null,error_class=null,error_message=null""",
                (candidates[0].key, exchange_date, request_key, Json({"universe_key": request.universe_key, "minimum_rows": request.minimum_rows})),
            )
        return None

    unchanged = await run_database_blocking(prepare_run)
    if unchanged:
        return unchanged
    provider_started_at = asyncio.get_running_loop().time()
    try:
        result = await call_tushare_api("stock_basic", params, fields, request.provider)
        rows = result.rows
        if looks_like_response_header(rows):
            raise provider_call_error("provider returned a header row instead of market reference data")
        valid_by_symbol = {
            str(row["ts_code"]).upper(): row for row in rows
            if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row.get("ts_code") or "").upper())
        }
        valid_rows = list(valid_by_symbol.values())
        if len(valid_rows) < request.minimum_rows:
            raise provider_call_error(f"stock_basic returned {len(valid_rows)} valid active symbols; expected at least {request.minimum_rows}")
        observed_at = datetime.now(timezone.utc)
        provider_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)

        def persist_result() -> int:
            with db.transaction() as connection:
                normalized = persist_tushare_rows(connection, "stock_basic", request_key, valid_rows, result.provider.key, observed_at)
                for row in valid_rows:
                    symbol = str(row["ts_code"]).upper()
                    connection.execute(
                        """INSERT INTO quant.universe_members(universe_key,symbol,enabled,priority,source,metadata,updated_at)
                           VALUES(%s,%s,true,1000,'stock-basic-all-a',%s,now())
                           ON CONFLICT(universe_key,symbol) DO UPDATE SET enabled=true,source=EXCLUDED.source,metadata=EXCLUDED.metadata,updated_at=now()""",
                        (request.universe_key, symbol, Json({"provider": result.provider.key, "reference_date": str(exchange_date)})),
                    )
                connection.execute(
                    """UPDATE quant.universe_members SET enabled=false,updated_at=now()
                         WHERE universe_key=%s AND source='stock-basic-all-a' AND enabled
                           AND NOT symbol = ANY(%s)""",
                    (request.universe_key, [str(row["ts_code"]).upper() for row in valid_rows]),
                )
                connection.execute("UPDATE quant.fetch_runs SET status='completed',row_count=%s,finished_at=now() WHERE request_key=%s", (len(valid_rows), request_key))
                record_provider_success(connection, result.provider.key, "stock_basic_all_a", len(valid_rows), provider_latency_ms)
                record_provider_api_capability(connection, result.provider.key, "stock_basic", "verified", len(valid_rows), "Full active A-share reference universe refreshed.")
                for provider_key, error in result.failed_providers:
                    record_provider_failure(connection, provider_key, "stock_basic", error, provider_latency_ms)
                    record_provider_api_capability(connection, provider_key, "stock_basic", "failed", note=error)
            return normalized

        normalized = await run_database_blocking(persist_result)
        return {"status": "completed", "universe_key": request.universe_key, "imported": len(valid_rows), "normalized_rows": normalized,
                "provider": result.provider.key, "request_key": request_key}
    except executor_saturated_error as error:
        await run_database_blocking(persist_tushare_fetch_blocked, request_key, error)
        return {"status": "blocked", "universe_key": request.universe_key, "reason": safe_error_detail(str(error), 500), "request_key": request_key}
    except Exception as error:  # noqa: BLE001 - provider failures are persisted and returned safely
        failure_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)

        def persist_failure() -> None:
            with db.transaction() as connection:
                detail = safe_error_detail(str(error), 1000)
                connection.execute("UPDATE quant.fetch_runs SET status='failed',finished_at=now(),error_class='provider_error',error_message=%s WHERE request_key=%s", (detail, request_key))
                record_provider_failure(connection, candidates[0].key, "stock_basic_all_a", detail, failure_latency_ms)
                record_provider_api_capability(connection, candidates[0].key, "stock_basic", "failed", note=detail)

        await run_database_blocking(persist_failure)
        return {"status": "blocked", "universe_key": request.universe_key, "reason": safe_error_detail(str(error), 500), "request_key": request_key}


__all__ = ["sync"]
