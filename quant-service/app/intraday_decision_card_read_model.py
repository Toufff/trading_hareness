"""Pure local projection for the latest intraday decision card.

Decision cards intentionally inspect only persisted evidence.  Keeping this
projection separate from the scanner makes that no-provider-call boundary
testable and prevents presentation changes from altering live signals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import HTTPException


def project_decision_card(
    symbol: str,
    *,
    quote: dict[str, Any],
    signal: dict[str, Any] | None,
    board_row: dict[str, Any] | None,
    analyst: dict[str, Any],
    strategy_market_state_fn: Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]],
    json_safe_fn: Callable[[Any], Any],
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Project a decision card from already-loaded local evidence only."""
    observed_at = quote["observed_at"]
    board_items = list((dict(board_row["payload"] or {}) if board_row else {}).get("items") or [])
    market_state, state_metrics = (
        strategy_market_state_fn(board_items)
        if board_items else ("unknown", {"reason": "no prior board snapshot"})
    )
    board_matches = []
    for item in board_items:
        for stock in item.get("top_stocks") or []:
            if str(stock.get("symbol") or "") == symbol:
                board_matches.append({
                    key: item.get(key)
                    for key in ("taxonomy_key", "sector_key", "label", "net_inflow", "change_pct")
                })
                break
    signal_type = str(signal["signal_type"]) if signal else "none"
    action = {
        "exit": "exit_risk_review",
        "reduce": "reduce_risk_review",
        "entry": "entry_research_review",
    }.get(signal_type, "observe")
    now = now_utc or datetime.now(timezone.utc)
    stale = now - observed_at > timedelta(minutes=3)
    risk_flags = list(signal["risk_flags"] or []) if signal else []
    if stale:
        risk_flags.append("stale_quote_no_realtime_action")
    return {
        "card_version": "intraday-decision-card-v1",
        "symbol": symbol,
        "observed_at": observed_at.isoformat(),
        "action": action,
        "decision_eligible": False,
        "quote": json_safe_fn(quote),
        "signal": json_safe_fn(signal) if signal else None,
        "market_state": market_state,
        "market_state_metrics": state_metrics,
        "board_matches": board_matches,
        "analyst_context": analyst,
        "risk_flags": sorted(set(risk_flags)),
        "notice": "研究/风控复核卡；必须由人工结合持仓、可卖数量和交易成本决策，系统不会自动下单。",
    }


def decision_card(
    connection: Any,
    symbol: str,
    *,
    strategy_market_state_fn: Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]],
    analyst_execution_context_fn: Callable[[Any, Any, datetime], dict[str, Any]],
    json_safe_fn: Callable[[Any], Any],
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Explain the latest saved signal without fetching a fresh market quote."""
    quote = connection.execute(
        """SELECT observed_at,source_name,price,pct_change,volume_ratio,turnover_rate,main_net_inflow
             FROM quant.intraday_quote_observations WHERE symbol=%s ORDER BY observed_at DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    if not quote:
        raise HTTPException(status_code=404, detail="no persisted intraday quote for symbol")
    quote = dict(quote)
    observed_at = quote["observed_at"]
    signal = connection.execute(
        """SELECT signal_event_id,signal_key,signal_type,severity,state,score,observed_at,expires_at,conditions,risk_flags
             FROM quant.intraday_signal_events WHERE symbol=%s AND observed_at<=%s
             ORDER BY observed_at DESC LIMIT 1""",
        (symbol, observed_at),
    ).fetchone()
    signal = dict(signal) if signal else None
    board_row = connection.execute(
        """SELECT observed_at,payload FROM quant.intraday_board_reports
             WHERE status='completed' AND observed_at<=%s ORDER BY observed_at DESC LIMIT 1""",
        (observed_at,),
    ).fetchone()
    board_row = dict(board_row) if board_row else None
    china_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    analyst = analyst_execution_context_fn(connection, china_date, observed_at)
    return project_decision_card(
        symbol, quote=quote, signal=signal, board_row=board_row, analyst=analyst,
        strategy_market_state_fn=strategy_market_state_fn, json_safe_fn=json_safe_fn, now_utc=now_utc,
    )


__all__ = ["decision_card", "project_decision_card"]
