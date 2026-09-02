"""Native-async, local-only decision-card projection for the dashboard."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .analyst_text_features import summary_from_rows
from .async_market_result_read_repository import ANALYST_SCORECARD_READINESS_SQL, analyst_scorecard_readiness
from .intraday_decision_card_read_model import project_decision_card
from .repo_common import async_fetch_all, async_fetch_one


async def _fetchall(connection: Any, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in await async_fetch_all(connection, query, params)]


async def _fetchone(connection: Any, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = await async_fetch_one(connection, query, params)
    return dict(row) if row else None


async def analyst_execution_context(
    connection: Any,
    as_of_date: date,
    observed_at: datetime,
    *,
    classify_text: Callable[[str], tuple[int, float, float]],
    factor_version: str,
    promotion_key: str,
    max_approved_weight: float,
) -> dict[str, Any]:
    """Read analyst display context from the same async transaction.

    This remains a zero-weight research prior unless the single promotion
    registry explicitly approves a capped weight; no live rule is changed.
    """
    lookback_days = 7
    earliest = as_of_date - timedelta(days=lookback_days - 1)
    reports = await _fetchall(
        connection,
        """SELECT r.remote_analyst_id,r.remote_report_id,r.summary,r.sections,
                  r.first_synced_at AS available_at,
                  coalesce(r.remote_published_at,r.remote_updated_at,r.remote_created_at) AS published_at
             FROM quant.remote_reports r
            WHERE (r.first_synced_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
              AND r.first_synced_at<=%s
            ORDER BY available_at DESC""",
        (earliest, as_of_date, observed_at),
    )
    claims = await _fetchall(
        connection,
        """SELECT c.remote_analyst_id,c.subject_key,c.subject_label,c.direction,c.strength,c.extraction_confidence,
                  c.available_at,e.remote_report_id,e.evidence_key
             FROM quant.analyst_claims c JOIN quant.analyst_evidence e ON e.evidence_id=c.evidence_id
            WHERE c.scope='theme' AND (c.available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
              AND c.available_at<=%s
              AND e.evidence_key = ANY(%s)""",
        (earliest, as_of_date, observed_at,
         ["summary", "section:market_view", "section:operation_guidance", "section:future_scenarios", "section:sectors_and_stocks"]),
    )
    summary = summary_from_rows(
        reports, claims, as_of_date,
        classify_text=classify_text, factor_version=factor_version, lookback_days=lookback_days,
    )
    registry = await _fetchone(
        connection,
        """SELECT methodology_version,status,approved_by,approved_at,max_live_weight,reason,evidence
             FROM quant.analyst_promotion_registry WHERE promotion_key=%s""",
        (promotion_key,),
    )
    if registry is None:
        promotion = {"execution_eligible": False, "weight": 0.0, "reason": "promotion_registry_missing",
                     "promotion_key": promotion_key, "as_of_date": str(as_of_date)}
    else:
        approved = registry.get("status") == "approved" and registry.get("approved_by") and registry.get("approved_at")
        weight = min(max_approved_weight, max(0.0, float(registry.get("max_live_weight") or 0))) if approved else 0.0
        promotion = {
            "execution_eligible": bool(weight > 0), "weight": weight,
            "reason": str(registry.get("reason") or ("approved" if weight else "promotion_not_approved")),
            "promotion_key": promotion_key, "methodology_version": registry.get("methodology_version"),
            "status": registry.get("status"), "approved_by": registry.get("approved_by"),
            "approved_at": registry.get("approved_at"), "as_of_date": str(as_of_date),
            "evidence": registry.get("evidence") or {},
        }
    scorecard_rows = await _fetchall(connection, ANALYST_SCORECARD_READINESS_SQL)
    readiness = analyst_scorecard_readiness(scorecard_rows)
    return {
        "factor_version": summary["factor_version"], "market": summary["market"], "themes": summary["themes"],
        "mature_analysts": [], "eligible_themes": [], "scorecard_readiness": readiness,
        "execution_eligible": promotion["execution_eligible"], "max_live_weight": promotion["weight"],
        "role": "small_prior" if promotion["execution_eligible"] else "research_context_only",
        "reason": promotion["reason"], "promotion": promotion, "data_boundary": summary["data_boundary"],
    }


async def decision_card(
    async_database: Any,
    symbol: str,
    *,
    strategy_market_state_fn: Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]],
    classify_text: Callable[[str], tuple[int, float, float]],
    factor_version: str,
    promotion_key: str,
    max_approved_weight: float,
    json_safe_fn: Callable[[Any], Any],
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Return a card using only the native async local read pool."""
    async with async_database.transaction() as connection:
        quote = await _fetchone(
            connection,
            """SELECT observed_at,source_name,price,pct_change,volume_ratio,turnover_rate,main_net_inflow
                 FROM quant.intraday_quote_observations WHERE symbol=%s ORDER BY observed_at DESC LIMIT 1""",
            (symbol,),
        )
        if quote is None:
            raise HTTPException(status_code=404, detail="no persisted intraday quote for symbol")
        observed_at = quote["observed_at"]
        signal = await _fetchone(
            connection,
            """SELECT signal_event_id,signal_key,signal_type,severity,state,score,observed_at,expires_at,conditions,risk_flags
                 FROM quant.intraday_signal_events WHERE symbol=%s AND observed_at<=%s
                 ORDER BY observed_at DESC LIMIT 1""",
            (symbol, observed_at),
        )
        board_row = await _fetchone(
            connection,
            """SELECT observed_at,payload FROM quant.intraday_board_reports
                 WHERE status='completed' AND observed_at<=%s ORDER BY observed_at DESC LIMIT 1""",
            (observed_at,),
        )
        china_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        analyst = await analyst_execution_context(
            connection, china_date, observed_at, classify_text=classify_text, factor_version=factor_version,
            promotion_key=promotion_key, max_approved_weight=max_approved_weight,
        )
    return project_decision_card(
        symbol, quote=quote, signal=signal, board_row=board_row, analyst=analyst,
        strategy_market_state_fn=strategy_market_state_fn, json_safe_fn=json_safe_fn, now_utc=now_utc,
    )


__all__ = ["analyst_execution_context", "decision_card"]
