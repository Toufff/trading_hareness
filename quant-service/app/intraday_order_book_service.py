"""Bounded Tencent order-book capture, isolated from FastAPI orchestration.

The service owns only the persisted evidence contract and one capture attempt.
The caller still owns scheduler cadence, market-session gating, leases and
provider wiring.  This keeps depth observations research-only and makes the
raw-to-feature write boundary reusable by replay tooling.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .order_book_features import order_book_observation


def enabled(environ: dict[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return values.get("INTRADAY_ORDER_BOOK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def interval_seconds(environ: dict[str, str] | None = None) -> float:
    values = os.environ if environ is None else environ
    try:
        return max(3.0, min(30.0, float(values.get("INTRADAY_ORDER_BOOK_INTERVAL_SECONDS", "3"))))
    except ValueError:
        return 3.0


def retention_days(environ: dict[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    try:
        return max(1, min(30, int(values.get("INTRADAY_ORDER_BOOK_RETENTION_DAYS", "7"))))
    except ValueError:
        return 7


def max_symbols(environ: dict[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    try:
        return max(1, min(80, int(values.get("INTRADAY_ORDER_BOOK_MAX_SYMBOLS", "40"))))
    except ValueError:
        return 40


def persist_observations(
    database: Any, observed_at: datetime, rows: list[dict[str, Any]], latency_ms: int,
    *, json_safe: Callable[[Any], Any], record_success: Callable[..., Any],
) -> int:
    """Persist source-qualified depth snapshots plus observational features."""
    stored = 0
    previous_cutoff = observed_at - timedelta(seconds=15)
    china_observed_at = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    session_start = china_observed_at.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    symbols = sorted({str(row.get("ts_code") or "") for row in rows if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row.get("ts_code") or ""))})
    with database.transaction() as connection:
        previous_rows = connection.execute(
            """SELECT DISTINCT ON(symbol) symbol,observed_at,raw
                 FROM quant.intraday_quote_observations
                WHERE symbol=ANY(%s) AND source_name='tencent_order_book'
                  AND observed_at>=%s AND observed_at<%s
                ORDER BY symbol,observed_at DESC""",
            (symbols, session_start, observed_at),
        ).fetchall() if symbols else []
        previous_by_symbol = {str(item["symbol"]): dict(item) for item in previous_rows}
        for row in rows:
            symbol = str(row.get("ts_code") or "")
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                continue
            previous = previous_by_symbol.get(symbol)
            previous_is_fresh = bool(previous and previous["observed_at"] >= previous_cutoff)
            features = order_book_observation(
                row, dict(previous["raw"] or {}) if previous_is_fresh else None,
            )
            if previous and not previous_is_fresh:
                features["delta_status"] = "stale_previous"
            raw = {**row, "order_book_features": features}
            inserted = connection.execute(
                """INSERT INTO quant.intraday_quote_observations(
                       scan_id,symbol,observed_at,source_name,price,pct_change,volume_ratio,turnover_rate,main_net_inflow,raw
                   ) VALUES(NULL,%s,%s,'tencent_order_book',%s,%s,NULL,NULL,NULL,%s)
                   ON CONFLICT(symbol,source_name,observed_at) DO NOTHING""",
                (symbol, observed_at, row.get("price"),
                 ((float(row["price"]) / float(row["pre_close"])) - 1) * 100 if row.get("pre_close") else None,
                 Json(json_safe(raw))),
            )
            stored += int(inserted.rowcount > 0)
        record_success(connection, "tencent_free", "order_book_quote", stored, latency_ms)
    return stored


def persist_failure(database: Any, error: str, latency_ms: int | None, *, record_failure: Callable[..., Any]) -> None:
    with database.transaction() as connection:
        record_failure(connection, "tencent_free", "order_book_quote", error, latency_ms)


async def capture_snapshot(
    symbols: list[str], *, max_symbols_value: int,
    fetch_quotes: Callable[..., Awaitable[list[dict[str, Any]]]],
    persist: Callable[[datetime, list[dict[str, Any]], int], Any],
    persist_error: Callable[[str, int | None], Any],
    run_database: Callable[..., Awaitable[Any]],
    safe_error: Callable[[str, int], str], handled_errors: tuple[type[BaseException], ...],
) -> dict[str, Any]:
    """Capture one bounded batch; callers choose the scheduled retry policy."""
    selected = list(dict.fromkeys(
        str(symbol).upper() for symbol in symbols
        if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(symbol).upper())
    ))[:max_symbols_value]
    if not selected:
        return {"status": "completed", "requested": 0, "stored": 0}
    started_at = asyncio.get_running_loop().time()
    observed_at = datetime.now(timezone.utc)
    try:
        rows = await fetch_quotes(selected, max_symbols=max_symbols_value)
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        stored = await run_database(persist, observed_at, rows, latency_ms)
        return {
            "status": "completed" if rows else "empty", "requested": len(selected), "received": len(rows),
            "stored": stored, "observed_at": observed_at.isoformat(), "latency_ms": latency_ms,
            "source": "tencent_single_quote_order_book",
        }
    except handled_errors as error:
        latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
        await run_database(persist_error, str(error)[:300], latency_ms)
        return {"status": "failed", "requested": len(selected), "stored": 0,
                "reason": safe_error(str(error), 300)}


__all__ = [
    "capture_snapshot", "enabled", "interval_seconds", "max_symbols", "persist_failure",
    "persist_observations", "retention_days",
]
