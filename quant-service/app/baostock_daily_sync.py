"""Dependency-injected BaoStock daily-bar fallback synchronization."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json

from .daily_bar_repository import shares_to_lots, yuan_to_thousand_yuan


def fetch_rows(symbols: list[str], trade_date: Any, *, baostock_code: Callable[[str], str]) -> tuple[list[dict[str, str]], list[str]]:
    """Blocking BaoStock client; caller dispatches it to the bounded public pool."""
    import baostock as bs

    login = bs.login()
    if str(login.error_code) != "0":
        return [], [f"login: {login.error_msg}"]
    rows: list[dict[str, str]] = []
    failures: list[str] = []
    fields = "date,code,open,high,low,close,preclose,volume,amount,isST"
    try:
        for symbol in symbols:
            result = bs.query_history_k_data_plus(
                baostock_code(symbol), fields, start_date=str(trade_date), end_date=str(trade_date), frequency="d", adjustflag="3",
            )
            if str(result.error_code) != "0":
                failures.append(f"{symbol}: {result.error_msg}")
                continue
            while result.next():
                rows.append(dict(zip(result.fields, result.get_row_data(), strict=True)))
    finally:
        bs.logout()
    return rows, failures


async def sync(
    request: Any,
    *,
    resolve_symbols: Callable[[list[str]], Awaitable[list[str]]],
    cn_today: Callable[[], Any],
    open_provider_capabilities: Callable[..., Awaitable[Any]],
    run_database_blocking: Callable[..., Awaitable[Any]],
    run_public_blocking: Callable[..., Awaitable[Any]],
    fetch_rows: Callable[..., tuple[list[dict[str, str]], list[str]]],
    baostock_code: Callable[[str], str],
    daily_bar_type: Any,
    decimal_or_none: Callable[[Any], Any],
    persist_daily_bar_batch: Callable[[list[Any]], int],
    db: Any,
    safe_error_detail: Callable[[str, int], str],
    record_provider_failure: Callable[..., Any],
    record_provider_success: Callable[..., Any],
    executor_saturated_error: type[Exception],
) -> dict[str, Any]:
    symbols = await resolve_symbols(request.symbols)
    if not symbols:
        return {"status": "disabled", "reason": "QUANT_UNIVERSE or explicit remote stock claims is not configured", "imported": 0}
    trade_date = request.trade_date or cn_today()
    request_key = hashlib.sha256(f"baostock:daily_bar:{trade_date}:{','.join(symbols)}".encode()).hexdigest()
    if "daily_bar" in await open_provider_capabilities("baostock", ["daily_bar"]):
        return {"status": "blocked", "reason": "provider health circuit is open; upstream request skipped",
                "trade_date": str(trade_date), "imported": 0, "failures": [], "request_key": request_key}

    def prepare_run() -> dict[str, Any] | None:
        with db.transaction() as connection:
            prior = connection.execute("SELECT status,row_count FROM quant.fetch_runs WHERE request_key=%s", (request_key,)).fetchone()
            if prior and prior["status"] == "completed":
                return {"status": "unchanged", "trade_date": str(trade_date), "imported": prior["row_count"], "request_key": request_key}
            connection.execute(
                """INSERT INTO quant.fetch_runs(provider_key,capability,trade_date,request_key,status,attempt_count,started_at,metadata)
                   VALUES('baostock','daily_bar',%s,%s,'running',1,now(),%s)
                   ON CONFLICT(request_key) DO UPDATE SET status='running',attempt_count=quant.fetch_runs.attempt_count+1,
                     started_at=now(),finished_at=null,error_class=null,error_message=null""",
                (trade_date, request_key, Json({"symbols": symbols})),
            )
        return None

    unchanged = await run_database_blocking(prepare_run)
    if unchanged:
        return unchanged
    provider_started_at = asyncio.get_running_loop().time()
    try:
        rows, failures = await run_public_blocking(fetch_rows, symbols, trade_date, baostock_code=baostock_code, timeout_seconds=30)
    except executor_saturated_error as error:
        detail = safe_error_detail(str(error), 300)

        def block_run() -> None:
            with db.transaction() as connection:
                connection.execute(
                    """UPDATE quant.fetch_runs SET status='blocked',row_count=0,finished_at=now(),
                       error_class='local_capacity',error_message=%s WHERE request_key=%s""", (detail, request_key),
                )
        await run_database_blocking(block_run)
        return {"status": "blocked", "trade_date": str(trade_date), "imported": 0,
                "failures": [], "reason": detail, "request_key": request_key}
    except Exception as error:  # noqa: BLE001 - provider errors become observable state
        rows, failures = [], [f"client: {str(error)[:300]}"]
    valid_bars: list[Any] = []
    for item in rows:
        try:
            code = str(item.get("code") or "")
            exchange, raw_code = code.split(".", 1)
            suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(exchange.lower())
            if not suffix:
                raise ValueError(f"unsupported code {code}")
            # BaoStock documents ``volume`` in raw shares and ``amount`` in
            # yuan; the canonical daily contract (matching Tushare) is lots
            # (100 shares) and thousand yuan.  Converting here, rather than
            # leaving the guard to catch it downstream, keeps every promoted
            # BaoStock row directly comparable to every other source's.
            valid_bars.append(daily_bar_type(
                symbol=f"{raw_code}.{suffix}", trading_date=datetime.strptime(str(item["date"]), "%Y-%m-%d").date(),
                open=decimal_or_none(item.get("open")), high=decimal_or_none(item.get("high")), low=decimal_or_none(item.get("low")),
                close=decimal_or_none(item.get("close")), pre_close=decimal_or_none(item.get("preclose")),
                volume=shares_to_lots(decimal_or_none(item.get("volume"))),
                amount=yuan_to_thousand_yuan(decimal_or_none(item.get("amount"))),
                is_st=str(item.get("isST", "0")) == "1", source="baostock",
            ))
        except Exception as error:  # noqa: BLE001 - retain malformed-row evidence
            failures.append(f"row: {str(error)[:180]}")
    imported = 0
    if valid_bars:
        try:
            imported = await run_database_blocking(persist_daily_bar_batch, valid_bars, timeout_seconds=60)
        except Exception as error:  # noqa: BLE001 - all validated rows share one atomic write unit
            failures.append(f"storage: {safe_error_detail(str(error), 180)}")
    if imported == 0 and not failures:
        failures.append("provider returned no daily bars")
    status = "completed" if not failures else "partial" if imported else "failed"
    finalize_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)

    def finalize_run() -> None:
        with db.transaction() as connection:
            connection.execute(
                """UPDATE quant.fetch_runs SET status=%s,row_count=%s,finished_at=now(),error_class=%s,error_message=%s WHERE request_key=%s""",
                (status, imported, "provider_error" if failures else None, " | ".join(failures)[:1000] if failures else None, request_key),
            )
            if failures:
                record_provider_failure(connection, "baostock", "daily_bar", " | ".join(failures), finalize_latency_ms)
            else:
                record_provider_success(connection, "baostock", "daily_bar", imported, finalize_latency_ms)

    await run_database_blocking(finalize_run)
    return {"status": status, "trade_date": str(trade_date), "imported": imported, "failures": failures, "request_key": request_key}


__all__ = ["fetch_rows", "sync"]
