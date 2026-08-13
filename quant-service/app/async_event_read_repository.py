"""Native async local market-event projections."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from fastapi import HTTPException


async def market_announcements(async_database: Any, symbol: str | None, limit: int, offset: int) -> dict[str, Any]:
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    async with async_database.transaction() as connection:
        if symbol:
            symbol = symbol.upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                raise HTTPException(status_code=422, detail="symbol must use the Tushare form, for example 000636.SZ")
            result = await connection.execute(
                """SELECT event_id,symbol,event_type,occurred_at,available_at,source,title,url,created_at
                     FROM quant.market_events WHERE symbol=%s ORDER BY occurred_at DESC,created_at DESC LIMIT %s OFFSET %s""",
                (symbol, limit, offset),
            )
            rows = await result.fetchall()
            total_result = await connection.execute("SELECT count(*)::int total FROM quant.market_events WHERE symbol=%s", (symbol,))
        else:
            result = await connection.execute(
                """SELECT event_id,symbol,event_type,occurred_at,available_at,source,title,url,created_at
                     FROM quant.market_events ORDER BY occurred_at DESC,created_at DESC LIMIT %s OFFSET %s""",
                (limit, offset),
            )
            rows = await result.fetchall()
            total_result = await connection.execute("SELECT count(*)::int total FROM quant.market_events")
        total = (await total_result.fetchone())["total"]
    return {"items": rows, "limit": limit, "offset": offset, "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


async def market_lhb_events(async_database: Any, trade_date: date | None, limit: int) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT event_id,symbol,event_type,occurred_at,available_at,source,title,url,created_at
                 FROM quant.market_events
                WHERE event_type='lhb_event' AND (%s::date IS NULL OR occurred_at::date=%s)
                ORDER BY occurred_at DESC,created_at DESC LIMIT %s""",
            (trade_date, trade_date, max(1, min(limit, 500))),
        )
        rows = await result.fetchall()
    return {"items": rows, "trade_date": str(trade_date) if trade_date else None,
            "notice": "龙虎榜为收盘后公开信息，仅进入下一交易日观察和复盘，不参与当天盘中评分。"}


__all__ = ["market_announcements", "market_lhb_events"]
