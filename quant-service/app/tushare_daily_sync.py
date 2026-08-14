"""Dependency-injected Tushare daily-bar synchronization."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json


async def sync(
    request: Any,
    *,
    resolve_symbols: Callable[[list[str]], Awaitable[list[str]]],
    provider_candidates: Callable[..., list[Any]],
    cn_today: Callable[[], Any],
    tushare_daily_api: Callable[[str], str],
    call_tushare_api: Callable[..., Awaitable[Any]],
    decimal_or_none: Callable[[Any], Any],
    daily_bar_type: Any,
    persist_daily_bar_batch: Callable[[list[Any]], int],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    record_provider_failure: Callable[..., Any],
    record_provider_success: Callable[..., Any],
    safe_error_detail: Callable[[str, int], str],
    executor_saturated_error: type[Exception],
) -> dict[str, Any]:
    symbols = await resolve_symbols(request.symbols)
    configured = provider_candidates("daily", "auto")
    if not configured or not symbols:
        return {"status": "disabled", "reason": "a Tushare market provider or QUANT_UNIVERSE is not configured", "imported": 0}
    trade_date = request.trade_date or request.end_date or cn_today()
    date_params = ({"trade_date": trade_date.strftime("%Y%m%d")} if request.start_date is None else {
        "start_date": request.start_date.strftime("%Y%m%d"), "end_date": request.end_date.strftime("%Y%m%d"),
    })
    provider_keys = [provider.key for provider in configured]
    request_key = hashlib.sha256(f"tushare:{','.join(provider_keys)}:daily_bar:{date_params}:{','.join(sorted(symbols))}".encode()).hexdigest()

    def prepare_run() -> dict[str, Any] | None:
        with db.transaction() as connection:
            prior = connection.execute("SELECT status,row_count FROM quant.fetch_runs WHERE request_key=%s", (request_key,)).fetchone()
            if prior and prior["status"] == "completed":
                range_start = request.start_date or trade_date
                range_end = request.end_date or trade_date
                coverage = connection.execute(
                    """SELECT count(DISTINCT symbol)::int covered FROM quant.canonical_bars_daily
                       WHERE symbol=ANY(%s) AND trading_date BETWEEN %s AND %s""",
                    (symbols, range_start, range_end),
                ).fetchone()["covered"]
                if coverage == len(symbols):
                    return {"status": "unchanged", "trade_date": str(trade_date), "imported": prior["row_count"], "request_key": request_key}
            connection.execute(
                """INSERT INTO quant.fetch_runs(provider_key,capability,trade_date,request_key,status,attempt_count,started_at,metadata)
                   VALUES(%s,'daily_bar',%s,%s,'running',1,now(),%s)
                   ON CONFLICT(request_key) DO UPDATE SET status='running',attempt_count=quant.fetch_runs.attempt_count+1,
                     started_at=now(),finished_at=null,error_class=null,error_message=null""",
                (provider_keys[0], trade_date, request_key, Json({"symbols": sorted(symbols), "provider_candidates": provider_keys, **date_params})),
            )
        return None

    unchanged = await run_database_blocking(prepare_run)
    if unchanged:
        return unchanged
    imported = 0
    failures: list[str] = []
    local_capacity_failures: list[str] = []
    provider_failures: list[tuple[str, str]] = []
    providers_used: set[str] = set()
    provider_latency_ms: dict[str, int] = {}
    sync_started_at = asyncio.get_running_loop().time()
    for symbol in symbols:
        provider_started_at = asyncio.get_running_loop().time()
        try:
            result = await call_tushare_api(
                tushare_daily_api(symbol), {"ts_code": symbol, **date_params},
                "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
            )
            elapsed_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)
            provider_failures.extend(result.failed_providers)
            for provider_key, _ in result.failed_providers:
                provider_latency_ms[provider_key] = elapsed_ms
            provider_latency_ms[result.provider.key] = elapsed_ms
            if not result.rows:
                failures.append(f"{symbol}: provider returned no daily bars")
                continue
            providers_used.add(result.provider.key)
            bars: list[Any] = []
            for data in result.rows:
                bars.append(daily_bar_type(
                    symbol=data["ts_code"], trading_date=datetime.strptime(data["trade_date"], "%Y%m%d").date(),
                    open=decimal_or_none(data.get("open")), high=decimal_or_none(data.get("high")),
                    low=decimal_or_none(data.get("low")), close=decimal_or_none(data.get("close")),
                    pre_close=decimal_or_none(data.get("pre_close")), volume=decimal_or_none(data.get("vol")),
                    amount=decimal_or_none(data.get("amount")), source=result.provider.key,
                ))
            imported += await run_database_blocking(persist_daily_bar_batch, bars, timeout_seconds=60)
        except executor_saturated_error as error:
            local_capacity_failures.append(f"{symbol}: {safe_error_detail(str(error), 180)}")
        except Exception as error:  # noqa: BLE001 - retain per-symbol sync failures
            failures.append(f"{symbol}: {str(error)[:180]}")
    if imported == 0 and not failures and not local_capacity_failures:
        failures.append("provider returned no daily bars")
    status = "blocked" if local_capacity_failures and imported == 0 and not failures else (
        "completed" if not failures else "partial" if imported else "failed"
    )
    finalize_latency_ms = round((asyncio.get_running_loop().time() - sync_started_at) * 1000)

    def finalize_run() -> None:
        with db.transaction() as connection:
            connection.execute(
                """UPDATE quant.fetch_runs SET status=%s,row_count=%s,finished_at=now(),error_class=%s,error_message=%s WHERE request_key=%s""",
                (status, imported, "local_capacity" if status == "blocked" else "provider_error" if failures else None,
                 " | ".join(local_capacity_failures if status == "blocked" else failures)[:1000] if (failures or local_capacity_failures) else None,
                 request_key),
            )
            for provider_key, error in provider_failures:
                record_provider_failure(connection, provider_key, "daily_bar", error, provider_latency_ms.get(provider_key, finalize_latency_ms))
            for provider_key in providers_used:
                record_provider_success(connection, provider_key, "daily_bar", imported, provider_latency_ms.get(provider_key, finalize_latency_ms))
            if failures and not providers_used:
                record_provider_failure(connection, provider_keys[0], "daily_bar", " | ".join(failures),
                                        provider_latency_ms.get(provider_keys[0], finalize_latency_ms))

    await run_database_blocking(finalize_run)
    return {"status": status, "trade_date": str(trade_date), "date_params": date_params, "imported": imported,
            "providers_used": sorted(providers_used), "failures": failures,
            "local_capacity_failures": local_capacity_failures, "request_key": request_key}


__all__ = ["sync"]
