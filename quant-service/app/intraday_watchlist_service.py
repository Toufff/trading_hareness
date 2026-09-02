"""Bounded write operations for the explicit intraday watchlist."""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .request_models import TushareFetchRequest, TushareSyncRequest

#: Model version stamped on every persisted factor snapshot; bump when the
#: factor family or its inputs change so old snapshots stay explainable.
WATCHLIST_FACTOR_MODEL_VERSION = "qlib-lean-watchlist-v1"


@dataclass(frozen=True)
class IntradayWatchlistDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    hydrate_history: Callable[[Any, str], Awaitable[dict[str, Any]]]
    exchange_for: Callable[[str], str]
    json_value: Callable[[Any], Any]
    http_exception: type[Exception]


@dataclass(frozen=True)
class WatchlistHistoryHydrationDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    sync_tushare: Callable[[TushareSyncRequest], Awaitable[dict[str, Any]]]
    fetch_supplemental: Callable[[str, TushareFetchRequest], Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]]]
    daily_factors: Callable[[str], dict[str, Any]]
    json_safe: Callable[[Any], Any]


async def hydrate_watchlist_history(
    watchlist_id: uuid.UUID, symbol: str, dependencies: WatchlistHistoryHydrationDependencies,
) -> dict[str, Any]:
    """Fetch bounded history on pool registration and persist factor evidence."""
    end_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start_date = end_date - timedelta(days=45)
    dated = {"ts_code": symbol, "start_date": start_date.strftime("%Y%m%d"), "end_date": end_date.strftime("%Y%m%d")}
    daily_result = await dependencies.sync_tushare(TushareSyncRequest(symbols=[symbol], start_date=start_date, end_date=end_date))
    supplemental = await asyncio.gather(
        dependencies.fetch_supplemental("watchlist_adj_factor", TushareFetchRequest(api_name="adj_factor", params=dated, max_rows=60)),
        dependencies.fetch_supplemental("watchlist_daily_basic", TushareFetchRequest(api_name="daily_basic", params=dated, max_rows=60)),
        dependencies.fetch_supplemental("watchlist_moneyflow", TushareFetchRequest(api_name="moneyflow", params=dated, max_rows=60)),
        dependencies.fetch_supplemental("watchlist_moneyflow_dc", TushareFetchRequest(api_name="moneyflow_dc", params=dated, max_rows=60)),
    )
    source_status = {"daily": daily_result, **{item[0]["source"]: item[0] for item in supplemental}}
    factors = await dependencies.run_database(dependencies.daily_factors, symbol)
    daily_ok = daily_result.get("status") in {"completed", "partial", "unchanged"} and int(factors.get("bar_count") or 0) >= 21
    supplemental_ok = sum(1 for item, _ in supplemental if item.get("status") in {"completed", "partial", "unchanged"})
    status = "completed" if daily_ok and supplemental_ok >= 2 else "partial" if daily_ok else "failed"
    factors.update({"factor_family": ["qlib_price_volume_rolling", "rsi14", "ma_trend", "lean_separate_risk_layer"],
                    "factor_ready": daily_ok, "supplemental_sources_ready": supplemental_ok})

    def persist_factor_snapshot() -> None:
        with dependencies.database.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.watchlist_factor_snapshots(watchlist_id,symbol,observed_at,lookback_calendar_days,status,source_status,factors,model_version)
                   VALUES(%s,%s,now(),45,%s,%s,%s,%s)""",
                (watchlist_id, symbol, status, Json(dependencies.json_safe(source_status)),
                 Json(dependencies.json_safe(factors)), WATCHLIST_FACTOR_MODEL_VERSION),
            )

    await dependencies.run_database(persist_factor_snapshot)
    return {"status": status, "start_date": str(start_date), "end_date": str(end_date), "source_status": source_status,
            "factors": factors, "notice": "因子用于盘中提醒分层与后续回测，不构成自动交易指令。"}


async def upsert(symbol: str, payload: Any, deps: IntradayWatchlistDependencies) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol != payload.symbol.upper():
        raise deps.http_exception(status_code=422, detail="path symbol must match payload symbol")
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise deps.http_exception(status_code=422, detail="symbol must use the Tushare form, for example 600176.SH")

    def persist_watchlist() -> Any:
        with deps.database.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.instruments(symbol,exchange,name,source) VALUES(%s,%s,%s,'intraday_watchlist')
                   ON CONFLICT(symbol) DO NOTHING""",
                (symbol, deps.exchange_for(symbol), payload.label),
            )
            return connection.execute(
                """INSERT INTO quant.intraday_watchlists(symbol,label,enabled,alert_on_entry,alert_on_exit,entry_price,available_quantity,hard_stop,take_profit,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(symbol) DO UPDATE SET label=EXCLUDED.label,enabled=EXCLUDED.enabled,alert_on_entry=EXCLUDED.alert_on_entry,
                      alert_on_exit=EXCLUDED.alert_on_exit,entry_price=EXCLUDED.entry_price,available_quantity=EXCLUDED.available_quantity,
                      hard_stop=EXCLUDED.hard_stop,take_profit=EXCLUDED.take_profit,metadata=EXCLUDED.metadata,updated_at=now()
                   RETURNING *""",
                (symbol, payload.label, payload.enabled, payload.alert_on_entry, payload.alert_on_exit, payload.entry_price,
                 payload.available_quantity, payload.hard_stop, payload.take_profit, deps.json_value(payload.metadata)),
            ).fetchone()

    row = await deps.run_database(persist_watchlist)
    history = await deps.hydrate_history(row["watchlist_id"], symbol)
    return {
        "item": row, "history_hydration": history,
        "notice": "已更新提醒范围并拉取了受限历史；不构成交易指令，也不会自动下单。",
    }


async def sync_history(symbol: str, deps: IntradayWatchlistDependencies) -> dict[str, Any]:
    symbol = symbol.upper()

    def load_watch() -> Any:
        with deps.database.transaction() as connection:
            return connection.execute(
                "SELECT watchlist_id,symbol FROM quant.intraday_watchlists WHERE symbol=%s", (symbol,),
            ).fetchone()

    watch = await deps.run_database(load_watch)
    if not watch:
        raise deps.http_exception(status_code=404, detail="watchlist symbol not found")
    return await deps.hydrate_history(watch["watchlist_id"], symbol)


async def delete(symbol: str, deps: IntradayWatchlistDependencies) -> dict[str, Any]:
    symbol = symbol.upper()

    def delete_watchlist() -> Any:
        with deps.database.transaction() as connection:
            return connection.execute(
                "DELETE FROM quant.intraday_watchlists WHERE symbol=%s RETURNING watchlist_id", (symbol,),
            ).fetchone()

    row = await deps.run_database(delete_watchlist)
    if not row:
        raise deps.http_exception(status_code=404, detail="watchlist symbol not found")
    return {"status": "deleted", "symbol": symbol}


__all__ = [
    "IntradayWatchlistDependencies",
    "WATCHLIST_FACTOR_MODEL_VERSION",
    "WatchlistHistoryHydrationDependencies",
    "delete",
    "hydrate_watchlist_history",
    "sync_history",
    "upsert",
]


