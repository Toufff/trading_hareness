"""Read-only local market-event projections for the research UI."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from fastapi import HTTPException


def market_announcements(database: Any, symbol: str | None, limit: int, offset: int) -> dict[str, Any]:
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    with database.transaction() as connection:
        if symbol:
            symbol = symbol.upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                raise HTTPException(status_code=422, detail="symbol must use the Tushare form, for example 000636.SZ")
            rows = connection.execute(
                """SELECT event_id,symbol,event_type,occurred_at,available_at,source,title,url,created_at
                     FROM quant.market_events WHERE symbol=%s ORDER BY occurred_at DESC,created_at DESC LIMIT %s OFFSET %s""",
                (symbol, limit, offset),
            ).fetchall()
            total = connection.execute("SELECT count(*)::int total FROM quant.market_events WHERE symbol=%s", (symbol,)).fetchone()["total"]
        else:
            rows = connection.execute(
                """SELECT event_id,symbol,event_type,occurred_at,available_at,source,title,url,created_at
                     FROM quant.market_events ORDER BY occurred_at DESC,created_at DESC LIMIT %s OFFSET %s""", (limit, offset),
            ).fetchall()
            total = connection.execute("SELECT count(*)::int total FROM quant.market_events").fetchone()["total"]
    return {"items": rows, "limit": limit, "offset": offset, "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


def market_lhb_events(database: Any, trade_date: date | None, limit: int) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT event_id,symbol,event_type,occurred_at,available_at,source,title,url,created_at
                 FROM quant.market_events
                WHERE event_type='lhb_event' AND (%s::date IS NULL OR occurred_at::date=%s)
                ORDER BY occurred_at DESC,created_at DESC LIMIT %s""",
            (trade_date, trade_date, max(1, min(limit, 500))),
        ).fetchall()
    return {"items": rows, "trade_date": str(trade_date) if trade_date else None,
            "notice": "龙虎榜为收盘后公开信息，仅进入下一交易日观察和复盘，不参与当天盘中评分。"}
