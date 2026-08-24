"""Bounded same-day control-plane synchronization for full-market daily bars.

This deliberately fetches one already-persisted trading date only.  It is not
part of the historical backfill path: its job is to keep a fresh daily bar's
adjustment factor, trading limits, fundamentals and suspension flag coherent
before strategy/review stages consume that date.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

CONTROL_APIS = ("adj_factor", "daily_basic", "stk_limit", "suspend_d")
CONTROL_PERSIST_TIMEOUT_SECONDS = 180
_A_SHARE = re.compile(r"\d{6}\.(SH|SZ|BJ)$")


def valid_rows(api_name: str, rows: list[dict[str, Any]], trade_date: date, parse_date: Callable[[Any], date | None]) -> list[dict[str, Any]]:
    """Keep only the requested A-share cross-section and remove duplicate codes."""
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "").upper()
        row_date = parse_date(row.get("trade_date") or row.get("suspend_date"))
        if _A_SHARE.fullmatch(symbol) and row_date == trade_date:
            by_symbol[symbol] = row
    return list(by_symbol.values())


async def sync(
    trade_date: date,
    *,
    expected_daily_rows: Callable[[date], int],
    call_tushare_api: Callable[..., Awaitable[Any]],
    parse_date: Callable[[Any], date | None],
    persist_tushare_rows: Callable[..., int],
    persist_blocked: Callable[..., Any],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    safe_error_detail: Callable[[str, int], str],
    executor_saturated_error: type[Exception],
    record_provider_success: Callable[..., Any],
    record_provider_failure: Callable[..., Any],
    record_provider_api_capability: Callable[..., Any],
) -> dict[str, Any]:
    """Fetch and promote exactly one date of controls after full-market daily.

    All three non-empty control cross-sections must cover at least 95% of the
    already persisted daily universe.  Suspension is legitimately empty and
    is used to establish the safe ``false`` baseline only after the other
    controls have passed their coverage gate.
    """
    expected = await run_database_blocking(expected_daily_rows, trade_date)
    if expected <= 0:
        return {"status": "blocked", "trade_date": str(trade_date), "reason": "full-market daily bars are not ready"}

    stamp = trade_date.strftime("%Y%m%d")
    started = asyncio.get_running_loop().time()
    results: dict[str, Any] = {}
    rows_by_api: dict[str, list[dict[str, Any]]] = {}
    try:
        for api_name in CONTROL_APIS:
            result = await call_tushare_api(api_name, {"trade_date": stamp}, None, "auto")
            rows = valid_rows(api_name, result.rows, trade_date, parse_date)
            if api_name != "suspend_d" and len(rows) < max(1, int(expected * 0.95)):
                raise ValueError(f"{api_name} returned {len(rows)} valid rows; expected at least 95% of {expected}")
            results[api_name] = result
            rows_by_api[api_name] = rows
    except executor_saturated_error as error:
        request_key = hashlib.sha256(json.dumps({"capability": "daily_controls_all_a", "trade_date": stamp}, sort_keys=True).encode()).hexdigest()
        await run_database_blocking(persist_blocked, request_key, error)
        return {"status": "blocked", "trade_date": str(trade_date), "reason": safe_error_detail(str(error), 500)}
    except Exception as error:  # provider result is intentionally not promoted partially
        return {"status": "blocked", "trade_date": str(trade_date), "reason": safe_error_detail(str(error), 500)}

    observed_at = datetime.now(timezone.utc)
    latency_ms = round((asyncio.get_running_loop().time() - started) * 1000)

    def persist() -> dict[str, int]:
        normalized: dict[str, int] = {}
        with db.transaction() as connection:
            # A valid, complete suspend_d response can be empty.  Reset only
            # this completed date, then its actual rows are re-applied below.
            connection.execute(
                "UPDATE quant.canonical_bars_daily SET is_suspended=false,canonicalized_at=now() WHERE trading_date=%s",
                (trade_date,),
            )
            connection.execute(
                "UPDATE quant.market_bars_daily SET is_suspended=false WHERE trading_date=%s",
                (trade_date,),
            )
            for api_name in CONTROL_APIS:
                result = results[api_name]
                request_key = hashlib.sha256(json.dumps({"capability": f"{api_name}_all_a", "trade_date": stamp, "provider": result.provider.key}, sort_keys=True).encode()).hexdigest()
                normalized[api_name] = persist_tushare_rows(
                    connection, api_name, request_key, rows_by_api[api_name], result.provider.key, observed_at,
                )
                record_provider_success(connection, result.provider.key, f"{api_name}_all_a", len(rows_by_api[api_name]), latency_ms)
                record_provider_api_capability(
                    connection, result.provider.key, api_name, "verified", len(rows_by_api[api_name]),
                    "Full-market same-day daily control plane refreshed.",
                )
                for provider_key, provider_error in result.failed_providers:
                    record_provider_failure(connection, provider_key, api_name, provider_error, latency_ms)
                    record_provider_api_capability(connection, provider_key, api_name, "failed", note=provider_error)
            # Normalization promotes controls directly to canonical bars.  The
            # source bar table is also a strategy/recovery input, so mirror
            # the verified same-provider controls there rather than leaving
            # its current date with NULLs.
            connection.execute(
                """UPDATE quant.market_bars_daily bar SET adj_factor=factor.adj_factor
                     FROM quant.daily_adjustment_factors factor
                    WHERE bar.trading_date=%s AND factor.trading_date=bar.trading_date
                      AND factor.symbol=bar.symbol AND factor.provider=%s""",
                (trade_date, results["adj_factor"].provider.key),
            )
            connection.execute(
                """UPDATE quant.market_bars_daily bar SET limit_up=limits.limit_up,limit_down=limits.limit_down
                     FROM quant.daily_trade_limits limits
                    WHERE bar.trading_date=%s AND limits.trading_date=bar.trading_date
                      AND limits.symbol=bar.symbol AND limits.provider=%s""",
                (trade_date, results["stk_limit"].provider.key),
            )
            connection.execute(
                """UPDATE quant.market_bars_daily bar SET is_suspended=true
                     FROM quant.security_suspensions suspension
                    WHERE bar.trading_date=%s AND suspension.suspend_date=%s
                      AND suspension.symbol=bar.symbol AND suspension.provider=%s""",
                (trade_date, trade_date, results["suspend_d"].provider.key),
            )
        return normalized

    # Four complete all-A payloads are promoted in one transaction.  The
    # general ten-second database budget is intentionally too small here and
    # can make a committed write look like a failed caller.  Keep a bounded,
    # explicit budget rather than relying on a worker that outlives its result.
    normalized = await run_database_blocking(persist, timeout_seconds=CONTROL_PERSIST_TIMEOUT_SECONDS)
    return {
        "status": "completed", "trade_date": str(trade_date), "expected_daily_rows": expected,
        "rows": {api_name: len(rows) for api_name, rows in rows_by_api.items()}, "normalized_rows": normalized,
        "providers": {api_name: result.provider.key for api_name, result in results.items()},
    }


__all__ = ["CONTROL_APIS", "CONTROL_PERSIST_TIMEOUT_SECONDS", "sync", "valid_rows"]
