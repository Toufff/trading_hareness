"""Persisted strategy-review projection, isolated from the FastAPI root."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .short_term_review import build_short_term_review


def build(
    connection: Any,
    request: Any,
    *,
    market_state: Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]],
    index_breadth_context: Callable[[Any, Any, str, datetime], dict[str, Any]],
    analyst_context: Callable[[Any, Any, datetime], dict[str, Any]],
    json_safe: Callable[[Any], Any],
    review_version: str = "strategy-loop-v1",
) -> dict[str, Any]:
    """Create a reproducible noon/close review from saved snapshots only."""
    as_of_date = request.as_of_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    time_filter = "" if request.session == "close" else "AND (observed_at AT TIME ZONE 'Asia/Shanghai')::time <= time '11:30'"
    row = connection.execute(
        f"""SELECT observed_at,summary,payload,source_status FROM quant.intraday_board_reports
             WHERE status='completed' AND (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s {time_filter}
             ORDER BY observed_at DESC LIMIT 1""",
        (as_of_date,),
    ).fetchone()
    if not row:
        return {"status": "blocked", "session": request.session, "as_of_date": str(as_of_date),
                "reason": "no persisted board snapshot for the requested checkpoint; no provider was called"}
    observed_at = row["observed_at"]
    payload = dict(row["payload"] or {})
    board_items = list(payload.get("items") or [])
    current_market_state, state_metrics = market_state(board_items)
    breadth = index_breadth_context(connection, as_of_date, request.session, observed_at)
    analyst = analyst_context(connection, as_of_date, observed_at)
    board_summary = dict(row["summary"] or {})
    event_rows = connection.execute(
        """SELECT event_type,symbol,occurred_at,available_at,source,title,body
             FROM quant.market_events
            WHERE (occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s
              AND available_at<=%s
              AND event_type = ANY(%s)
            ORDER BY occurred_at,symbol""",
        (as_of_date, observed_at, ["limit_up_pool", "limit_down_pool", "previous_limit_pool", "lhb_event"]),
    ).fetchall()
    daily_rows = connection.execute(
        """SELECT b.symbol,i.name,b.amount,
                      CASE WHEN b.pre_close IS NOT NULL AND b.pre_close<>0
                           THEN (b.close/b.pre_close-1)*100 ELSE NULL END AS pct_chg
                 FROM quant.canonical_bars_daily b
            LEFT JOIN quant.instruments i ON i.symbol=b.symbol
                WHERE b.trading_date=%s
                ORDER BY b.amount DESC NULLS LAST""",
        (as_of_date,),
    ).fetchall()
    short_term_review = build_short_term_review(
        event_rows=[dict(item) for item in event_rows],
        daily_rows=[dict(item) for item in daily_rows],
        board_summary=board_summary,
        observed_at=observed_at.isoformat(),
    )
    review = {
        "status": "completed", "review_version": review_version, "session": request.session,
        "as_of_date": str(as_of_date), "observed_at": observed_at.isoformat(),
        "market_state": current_market_state, "market_state_metrics": state_metrics,
        "index_breadth_context": breadth, "board_flow": board_summary, "analyst_context": analyst,
        "short_term_review": short_term_review,
        "playbook": {
            "entry": "only research candidates aligned with market state, board flow and two-scan price/volume confirmation",
            "exit": "hard stop first; then reduce on confirmed price/VWAP and flow reversal",
            "next_session": "龙虎榜、公告和新闻 are context only; they never revise same-day intraday evidence",
        },
        "data_boundary": {
            "board_flow": "persisted Eastmoney/Tencent snapshot",
            "index_breadth": "saved Tencent all-A breadth plus point-in-time SSE/CSI300/SZSE/ChiNext close-daily context",
            "tushare": "daily flow is close-context; rt_min is stock validation only",
            "analyst": "text-only reports available no later than observed_at",
            "automation": "no broker order submission",
        },
    }
    if request.persist:
        review_key = hashlib.sha256(
            f"{review_version}:{request.session}:{as_of_date}:{observed_at.isoformat()}".encode()
        ).hexdigest()
        connection.execute(
            """INSERT INTO quant.strategy_review_runs(review_key,exchange_date,session,observed_at,market_state,data_boundary,report)
               VALUES(%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(review_key) DO UPDATE SET market_state=EXCLUDED.market_state,data_boundary=EXCLUDED.data_boundary,report=EXCLUDED.report""",
            (review_key, as_of_date, request.session, observed_at, current_market_state,
             Json(json_safe(review["data_boundary"])), Json(json_safe(review))),
        )
        review["review_key"] = review_key
    return review


__all__ = ["build"]
