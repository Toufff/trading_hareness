"""Point-in-time analyst action markers aligned to locally stored minute bars."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo


CN = ZoneInfo("Asia/Shanghai")
SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


def _json_time(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _event_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(CN).date() if value.tzinfo else value.date()
    return date.fromisoformat(str(value))


def _nearest_bar(event_at: datetime | None, bars: list[dict[str, Any]]) -> dict[str, Any]:
    if event_at is None or not bars:
        return {"mapping_status": "no_event_or_bar"}
    nearest = min(bars, key=lambda row: abs((row["bar_time"] - event_at).total_seconds()))
    offset = (nearest["bar_time"] - event_at).total_seconds()
    if abs(offset) > 180:
        return {"mapping_status": "outside_three_minute_window", "nearest_bar_time": _json_time(nearest["bar_time"]), "offset_seconds": int(offset)}
    return {
        "mapping_status": "mapped",
        "nearest_bar_time": _json_time(nearest["bar_time"]),
        "nearest_bar_close": float(nearest["close"]),
        "offset_seconds": int(offset),
    }


def analyst_stock_timeline(
    database: Any,
    *,
    symbol: str,
    start_date: date | None = None,
    end_date: date | None = None,
    analyst_id: str | None = None,
    limit: int = 1500,
) -> dict[str, Any]:
    symbol = str(symbol or "").upper().strip()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must use the 000001.SZ format")
    with database.transaction() as connection:
        latest_available = connection.execute(
            """SELECT max(trading_date) AS latest_date
                 FROM quant.intraday_minute_sessions
                WHERE symbol=%s""", (symbol,)
        ).fetchone()["latest_date"]
    end = end_date or latest_available or datetime.now(CN).date()
    start = start_date or end
    if end < start or (end - start).days > 31:
        raise ValueError("timeline window must be ordered and no longer than 31 days")
    bounded_limit = max(60, min(int(limit), 3000))
    with database.transaction() as connection:
        bars = [dict(row) for row in connection.execute(
            """SELECT bar_time,open,high,low,close,volume,amount,source_name,available_at
                 FROM quant.intraday_minute_sessions
                WHERE symbol=%s AND trading_date BETWEEN %s AND %s
                ORDER BY bar_time DESC LIMIT %s""", (symbol, start, end, bounded_limit)
        ).fetchall()]
        bar_source = "intraday_minute_sessions"
        if not bars:
            bars = [dict(row) for row in connection.execute(
                """SELECT bar_time,open,high,low,close,volume,amount,source_name,available_at
                     FROM quant.market_bars_minute
                    WHERE symbol=%s AND (bar_time AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                    ORDER BY bar_time DESC LIMIT %s""", (symbol, start, end, bounded_limit)
            ).fetchall()]
            bar_source = "market_bars_minute"
        bars.reverse()
        actions = [dict(row) for row in connection.execute(
            """SELECT action_id::text AS event_id,remote_analyst_id AS analyst_id,symbol,label,
                          action_type AS action,direction,stated_at,event_time,available_at,evidence,
                          'author_trade_action' AS source_kind,true AS replay_only
                 FROM (
                    SELECT a.*,a.stated_at AS event_time
                      FROM quant.analyst_trade_actions a
                     WHERE a.symbol=%s
                       AND (a.stated_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                       AND (%s::text IS NULL OR a.remote_analyst_id=%s)
                 ) author_actions
                UNION ALL
               SELECT observation_id::text AS event_id,analyst_id,subject_key AS symbol,subject_label AS label,
                      action,direction,stated_at,coalesce(stated_at,strategy_available_at) AS event_time,
                      strategy_available_at AS available_at,evidence_span AS evidence,
                      source_kind,false AS replay_only
                 FROM quant.analyst_observations
                WHERE scope='stock' AND subject_key=%s
                  AND (coalesce(stated_at,strategy_available_at) AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND (%s::text IS NULL OR analyst_id=%s)
                ORDER BY event_time,event_id""",
            (symbol, start, end, analyst_id, analyst_id, symbol, start, end, analyst_id, analyst_id),
        ).fetchall()]
    action_markers: list[dict[str, Any]] = []
    for row in actions:
        event_at = row.get("event_time")
        marker = {
            "event_id": row.get("event_id"), "analyst_id": row.get("analyst_id"),
            "symbol": row.get("symbol"), "label": row.get("label") or symbol,
            "action": row.get("action"), "direction": row.get("direction"),
            "event_time": _json_time(event_at), "stated_at": _json_time(row.get("stated_at")),
            "available_at": _json_time(row.get("available_at")), "evidence": row.get("evidence"),
            "source_kind": row.get("source_kind"), "replay_only": bool(row.get("replay_only")),
            "time_basis": "stated_at" if row.get("stated_at") is not None else "strategy_available_at",
        }
        marker.update(_nearest_bar(event_at, bars))
        action_markers.append(marker)
    return {
        "symbol": symbol, "start_date": str(start), "end_date": str(end), "timezone": "Asia/Shanghai",
        "bar_source": bar_source, "bar_count": len(bars), "action_count": len(action_markers),
        "bars": [{**row, "bar_time": _json_time(row.get("bar_time")), "available_at": _json_time(row.get("available_at"))} for row in bars],
        "actions": action_markers,
        "boundary": "actions use stated_at when present; strategy_available_at is retained for point-in-time audit; no media is fetched",
    }


__all__ = ["analyst_stock_timeline"]
