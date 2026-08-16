"""Bounded Tencent minute-session capture for explicit watchlist symbols."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


class IntradayMinuteCaptureActions:
    """Persist only current-session minute profiles, never a broad minute archive."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def capture(
        self,
        symbols: list[str],
        *,
        realtime_session: Callable[[], Awaitable[tuple[bool, str]]],
        fetch_minutes: Callable[[str], Awaitable[list[dict[str, Any]]]],
        run_database: Callable[..., Awaitable[Any]],
        parse_minute: Callable[[dict[str, Any]], dict[str, Any]],
        ensure_instrument: Callable[[Any, str], None],
        retention_days: Callable[[], int],
    ) -> dict[str, Any]:
        """Capture current-session Tencent rows through an injected persistence seam."""
        active, reason = await realtime_session()
        if not active:
            return {"status": "blocked", "reason": reason, "stored": 0, "symbols": symbols}
        local_now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        trading_date = local_now.date()

        async def fetch_one(symbol: str) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, str | None]:
            try:
                rows = await fetch_minutes(symbol)
                return symbol, rows, {"provider": "tencent_free", "api_name": "minute/query", "status": "completed"}, None
            # Transport ownership stays inside the injected provider adapter.
            # This action only turns its bounded failure into per-symbol
            # research evidence; it must not import or classify HTTP clients.
            except Exception as error:  # noqa: BLE001 - one symbol must not stop the bounded basket
                return symbol, [], None, str(error)[:300]

        results = await asyncio.gather(*(fetch_one(symbol) for symbol in symbols), return_exceptions=True)
        keep_days = retention_days()
        retention_start = trading_date - timedelta(days=keep_days)

        def persist_sessions() -> tuple[dict[str, int], dict[str, str], dict[str, Any]]:
            stored_by_symbol: dict[str, int] = {}
            errors: dict[str, str] = {}
            source_status: dict[str, Any] = {}
            with self._database.transaction() as connection:
                for result in results:
                    if isinstance(result, Exception):
                        errors["unknown"] = str(result)[:300]
                        continue
                    symbol, rows, source, error = result
                    if error:
                        errors[symbol] = error
                        continue
                    source_status[symbol] = source
                    ensure_instrument(connection, symbol)
                    stored = 0
                    for raw_row in rows:
                        try:
                            minute_clock = str(raw_row.get("time") or "")
                            if re.fullmatch(r"\d{4}", minute_clock):
                                minute_clock = f"{minute_clock[:2]}:{minute_clock[2:]}"
                            # Tencent exposes close/cumulative turnover, not
                            # true minute OHLC.  Use a flat bar only for the
                            # same-clock profile and preserve the raw source.
                            row = parse_minute({
                                **raw_row, "ts_code": symbol,
                                "datetime": f"{trading_date.isoformat()} {minute_clock}",
                                "open": raw_row.get("close"), "high": raw_row.get("close"), "low": raw_row.get("close"),
                            })
                            local_bar_time = row["bar_time"].astimezone(ZoneInfo("Asia/Shanghai"))
                            if local_bar_time.date() != trading_date:
                                continue
                            bucket = local_bar_time.strftime("%H:%M")
                            connection.execute(
                                """INSERT INTO quant.intraday_minute_sessions(
                                       symbol,trading_date,minute_bucket,bar_time,open,high,low,close,volume,amount,source_name,available_at,raw
                                   ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'tencent_intraday_minutes',%s,%s)
                                   ON CONFLICT(symbol,trading_date,minute_bucket,source_name) DO UPDATE SET
                                       bar_time=EXCLUDED.bar_time,open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                                       close=EXCLUDED.close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,
                                       available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
                                (symbol, trading_date, bucket, row["bar_time"], row["open"], row["high"], row["low"], row["close"],
                                 row["volume"], row["amount"], datetime.now(timezone.utc), Json(row["raw"])),
                            )
                            stored += 1
                        except (ValueError, TypeError) as validation_error:
                            errors.setdefault(symbol, f"invalid minute row: {str(validation_error)[:200]}")
                    stored_by_symbol[symbol] = stored
                    connection.execute(
                        """DELETE FROM quant.intraday_minute_sessions
                             WHERE symbol=%s AND trading_date<%s
                               AND source_name IN ('tushare_super_get_rt_min_daily','tushare_super_rt_min_daily','tencent_intraday_minutes')""",
                        (symbol, retention_start),
                    )
            return stored_by_symbol, errors, source_status

        stored_by_symbol, errors, source_status = await run_database(persist_sessions, timeout_seconds=60)
        stored_total = sum(stored_by_symbol.values())
        status = "completed" if stored_total and not errors else "partial" if stored_total else "failed"
        return {
            "status": status, "trading_date": str(trading_date), "symbols": symbols, "stored": stored_total,
            "stored_by_symbol": stored_by_symbol, "errors": errors, "source_status": source_status,
            "retention_days": keep_days,
            "notice": "仅保存显式观察池的盘末分钟剖面，用于历史同刻量能基线；不构成全市场分钟归档。",
        }
