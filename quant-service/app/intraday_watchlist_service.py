"""Bounded write operations for the explicit intraday watchlist."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class IntradayWatchlistDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    hydrate_history: Callable[[Any, str], Awaitable[dict[str, Any]]]
    exchange_for: Callable[[str], str]
    json_value: Callable[[Any], Any]
    http_exception: type[Exception]


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


__all__ = ["IntradayWatchlistDependencies", "delete", "sync_history", "upsert"]
