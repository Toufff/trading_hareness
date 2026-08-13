"""Read-only strategy health and drift projection.

This module deliberately reads only evidence already persisted by the live
service.  It is a control-plane view: a warning can keep a rule descriptive
or trigger manual review, but it never changes a threshold or analyst weight.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _age_seconds(value: Any, now: datetime) -> float | None:
    if value is None:
        return None
    observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return max(0.0, (now - observed).total_seconds())


def latest_strategy_health(database: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Return bounded live-evidence health without any upstream call."""
    now = now or datetime.now(timezone.utc)
    with database.transaction() as connection:
        counts = connection.execute(
            """SELECT
                (SELECT count(*)::int FROM quant.intraday_signal_events
                  WHERE observed_at >= now()-interval '7 days') AS signals_7d,
                (SELECT count(*)::int FROM quant.intraday_signal_events
                  WHERE observed_at >= now()-interval '14 days'
                    AND observed_at < now()-interval '7 days') AS signals_prior_7d,
                (SELECT count(DISTINCT episode_id)::int FROM quant.intraday_signal_events
                  WHERE observed_at >= now()-interval '7 days' AND episode_id IS NOT NULL) AS episodes_7d,
                (SELECT count(DISTINCT e.signal_event_id)::int
                   FROM quant.intraday_signal_outcomes o
                   JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured'
                    AND e.observed_at >= now()-interval '7 days') AS matured_30m_7d,
                (SELECT count(DISTINCT (e.observed_at AT TIME ZONE 'Asia/Shanghai')::date)::int
                   FROM quant.intraday_signal_outcomes o
                   JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured'
                    AND e.observed_at >= now()-interval '7 days') AS matured_days_7d"""
        ).fetchone()
        outcomes = connection.execute(
            """SELECT count(*)::int AS rows,
                      count(*) FILTER (WHERE raw_return > 0)::int AS positive,
                      avg(raw_return) AS avg_return
                 FROM quant.intraday_signal_outcomes
                WHERE horizon_key='30m' AND status='matured'
                  AND calculated_at >= now()-interval '7 days'"""
        ).fetchone()
        latest_quotes = connection.execute(
            """SELECT max(observed_at) AS latest_quote_at,
                      count(*) FILTER (WHERE observed_at >= now()-interval '90 seconds')::int AS fresh_quote_rows
                 FROM quant.intraday_quote_observations"""
        ).fetchone()
        strategy_rows = connection.execute(
            """SELECT signal_key AS strategy_key,count(*)::int AS signals,
                      count(DISTINCT episode_id)::int AS episodes
                 FROM quant.intraday_signal_events
                WHERE observed_at >= now()-interval '7 days'
                GROUP BY strategy_key ORDER BY signals DESC,strategy_key"""
        ).fetchall()

    signals_7d = int((counts or {}).get("signals_7d") or 0)
    signals_prior = int((counts or {}).get("signals_prior_7d") or 0)
    drift_ratio = signals_7d / signals_prior if signals_prior else None
    drift_status = "insufficient_baseline" if signals_prior == 0 else (
        "warning" if drift_ratio > 3.0 or drift_ratio < 0.33 else "stable"
    )
    matured = int((counts or {}).get("matured_30m_7d") or 0)
    trading_days = int((counts or {}).get("matured_days_7d") or 0)
    gate_status = "ready_for_formal_validation" if matured >= 200 and trading_days >= 60 else "accumulating"
    quote_age = _age_seconds((latest_quotes or {}).get("latest_quote_at"), now)
    quote_status = "fresh" if quote_age is not None and quote_age <= 90 else "stale_or_missing"
    rows = int((outcomes or {}).get("rows") or 0)
    positive = int((outcomes or {}).get("positive") or 0)
    return {
        "status": "research_only",
        "observed_at": now,
        "window": {"current_days": 7, "baseline_days": 7},
        "trigger_frequency": {
            "signals_7d": signals_7d, "signals_prior_7d": signals_prior,
            "episodes_7d": int((counts or {}).get("episodes_7d") or 0),
            "drift_ratio": round(drift_ratio, 4) if drift_ratio is not None else None,
            "drift_status": drift_status,
        },
        "outcomes_30m": {
            "matured": matured, "trading_days": trading_days, "rows": rows,
            "positive_rate": round(positive / rows, 4) if rows else None,
            "avg_directional_return": (float((outcomes or {}).get("avg_return"))
                                        if (outcomes or {}).get("avg_return") is not None else None),
        },
        "data_freshness": {
            "latest_quote_at": (latest_quotes or {}).get("latest_quote_at"),
            "quote_age_seconds": round(quote_age, 1) if quote_age is not None else None,
            "fresh_quote_rows": int((latest_quotes or {}).get("fresh_quote_rows") or 0),
            "status": quote_status,
        },
        "strategy_breakdown": [dict(row) for row in strategy_rows],
        "validation_gate": {
            "status": gate_status, "required_matured_signals": 200,
            "required_trading_days": 60, "live_effect": "none",
        },
        "notice": "健康/漂移只用于研究监控；不会在线调参、晋级或改变分析师权重。",
    }


__all__ = ["latest_strategy_health"]
