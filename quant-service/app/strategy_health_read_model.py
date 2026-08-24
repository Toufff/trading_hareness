"""Read-only strategy health and drift projection.

This module deliberately reads only evidence already persisted by the live
service.  It is a control-plane view: a warning can keep a rule descriptive
or trigger manual review, but it never changes a threshold or analyst weight.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .episode_lifecycle import strategy_family


CN_TZ = ZoneInfo("Asia/Shanghai")
HEALTH_WINDOW_DAYS = 7
FORMAL_VALIDATION_MIN_MATURED_SIGNALS = 200
FORMAL_VALIDATION_MIN_TRADING_DAYS = 60


def _age_seconds(value: Any, now: datetime) -> float | None:
    if value is None:
        return None
    observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return max(0.0, (now - observed).total_seconds())


def market_session_projection(now: datetime, calendar_is_open: Any) -> dict[str, Any]:
    """Describe whether quote freshness is expected at the exchange clock.

    This is intentionally a pure projection so health checks do not mistake
    the noon recess, weekends, or a persisted exchange closure for a provider
    outage.  A missing calendar entry remains conservative during continuous
    auction: quote freshness is still required and can surface a real issue.
    """
    local = now.astimezone(CN_TZ)
    current = local.time()
    calendar_open = None if calendar_is_open is None else bool(calendar_is_open)
    if calendar_open is False:
        return {
            "status": "market_closed", "quote_required": False,
            "calendar_is_open": False, "reason": "SSE trade calendar marks this date closed",
        }
    if local.weekday() >= 5:
        return {
            "status": "market_closed", "quote_required": False,
            "calendar_is_open": calendar_open, "reason": "SSE is closed on weekends",
        }
    if time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0):
        if calendar_open is None:
            return {
                "status": "calendar_unknown", "quote_required": True,
                "calendar_is_open": None, "reason": "continuous auction but SSE calendar has no entry",
            }
        return {
            "status": "continuous_auction", "quote_required": True,
            "calendar_is_open": True, "reason": "within SSE continuous auction session",
        }
    if time(11, 30) < current < time(13, 0):
        status, reason = "lunch_break", "within SSE noon recess"
    elif current < time(9, 30):
        status, reason = "pre_open", "before SSE continuous auction"
    else:
        status, reason = "post_close", "after SSE continuous auction"
    return {
        "status": status, "quote_required": False,
        "calendar_is_open": calendar_open, "reason": reason,
    }


def _drift(current: int, previous: int) -> tuple[float | None, str]:
    if previous <= 0:
        return None, "insufficient_baseline"
    ratio = current / previous
    return ratio, "warning" if ratio > 3.0 or ratio < 0.33 else "stable"


def health_recommendation(*, drift_status: str, quote_status: str, gate_status: str,
                          matured: int, trading_days: int) -> dict[str, Any]:
    """Translate read-only health facts into an auditable, non-mutating action.

    This is deliberately not a circuit breaker: live policy and promotion
    registry remain the only authorities that can block a signal or apply an
    analyst weight.  The returned action is for operators and the dashboard.
    """
    flags: list[str] = []
    if quote_status == "stale_or_missing":
        flags.append("quote_freshness_degraded")
    if drift_status == "warning":
        flags.append("trigger_frequency_drift")
    if gate_status != "ready_for_formal_validation":
        flags.append("validation_sample_insufficient")
    if quote_status == "stale_or_missing":
        action = "freeze_new_entries"
    elif drift_status == "warning":
        action = "manual_review"
    elif gate_status != "ready_for_formal_validation":
        action = "keep_descriptive_only"
    else:
        action = "monitor"
    return {
        "action": action,
        "flags": flags,
        "matured_signals": int(matured),
        "trading_days": int(trading_days),
        "live_effect": "none",
        "notice": "仅生成运营建议；不会在线调参、晋级策略或改变分析师权重。",
    }


def strategy_family_breakdown(rows: list[Any]) -> list[dict[str, Any]]:
    """Collapse symbol-level signal keys into auditable strategy families.

    Health is intended to reveal rule-level trigger drift.  Returning one row
    per watched symbol turns that signal into a long stock list and can double
    count lifecycle episodes.  The database provides exact distinct ids for
    production reads; the numeric fallback keeps compatibility projections and
    focused unit fixtures tolerant of older result shapes.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        family = strategy_family(str(row.get("strategy_key") or ""))
        item = grouped.setdefault(family, {"strategy_key": family, "strategy_family": family,
                                           "signals": 0, "_episode_ids": set(), "_episode_fallback": 0})
        item["signals"] += int(row.get("signals") or 0)
        ids = row.get("episode_ids")
        if isinstance(ids, (list, tuple, set)):
            item["_episode_ids"].update(str(value) for value in ids if value is not None)
        else:
            item["_episode_fallback"] += int(row.get("episodes") or 0)
    result = []
    for item in grouped.values():
        exact = len(item.pop("_episode_ids"))
        fallback = int(item.pop("_episode_fallback"))
        item["episodes"] = exact if exact else fallback
        result.append(item)
    return sorted(result, key=lambda item: (-int(item["episodes"]), -int(item["signals"]), str(item["strategy_key"])))


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
                (SELECT count(DISTINCT episode_id)::int FROM quant.intraday_signal_events
                  WHERE observed_at >= now()-interval '14 days' AND observed_at < now()-interval '7 days'
                    AND episode_id IS NOT NULL) AS episodes_prior_7d,
                (SELECT count(DISTINCT e.signal_event_id)::int
                   FROM quant.intraday_signal_outcomes o
                   JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured'
                    AND e.observed_at >= now()-interval '7 days') AS matured_30m_7d,
                (SELECT count(DISTINCT (e.observed_at AT TIME ZONE 'Asia/Shanghai')::date)::int
                   FROM quant.intraday_signal_outcomes o
                   JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured'
                    AND e.observed_at >= now()-interval '7 days') AS matured_days_7d,
                (SELECT count(DISTINCT e.signal_event_id)::int
                   FROM quant.intraday_signal_outcomes o
                   JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured') AS matured_30m_total,
                (SELECT count(DISTINCT (e.observed_at AT TIME ZONE 'Asia/Shanghai')::date)::int
                   FROM quant.intraday_signal_outcomes o
                   JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                  WHERE o.horizon_key='30m' AND o.status='matured') AS matured_days_total"""
        ).fetchone()
        outcomes = connection.execute(
            """SELECT count(DISTINCT e.signal_event_id)::int AS rows,
                      count(DISTINCT e.signal_event_id) FILTER (WHERE o.raw_return > 0)::int AS positive,
                      avg(o.raw_return) AS avg_return
                 FROM quant.intraday_signal_outcomes o
                 JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                WHERE o.horizon_key='30m' AND o.status='matured'
                  AND e.observed_at >= now()-interval '7 days'"""
        ).fetchone()
        latest_quotes = connection.execute(
            """SELECT max(observed_at) AS latest_quote_at,
                      count(*) FILTER (WHERE observed_at >= now()-interval '90 seconds')::int AS fresh_quote_rows,
                      (SELECT is_open FROM quant.market_trade_calendar
                        WHERE exchange='SSE'
                          AND calendar_date=(now() AT TIME ZONE 'Asia/Shanghai')::date) AS calendar_is_open
                 FROM quant.intraday_quote_observations"""
        ).fetchone()
        strategy_rows = connection.execute(
            """SELECT signal_key AS strategy_key,count(*)::int AS signals,
                      array_agg(DISTINCT episode_id) FILTER (WHERE episode_id IS NOT NULL) AS episode_ids
                 FROM quant.intraday_signal_events
                WHERE observed_at >= now()-interval '7 days'
                GROUP BY strategy_key ORDER BY signals DESC,strategy_key"""
        ).fetchall()

    return strategy_health_payload_from_rows(counts, outcomes, latest_quotes, strategy_rows, now=now)


def strategy_health_payload_from_rows(counts: Any, outcomes: Any, latest_quotes: Any,
                                      strategy_rows: list[Any], *, now: datetime) -> dict[str, Any]:
    """Assemble the control-plane response from already-read local rows."""
    signals_7d = int((counts or {}).get("signals_7d") or 0)
    signals_prior = int((counts or {}).get("signals_prior_7d") or 0)
    episodes_7d = int((counts or {}).get("episodes_7d") or 0)
    episodes_prior = int((counts or {}).get("episodes_prior_7d") or 0)
    raw_signal_drift_ratio, raw_signal_drift_status = _drift(signals_7d, signals_prior)
    if episodes_7d and episodes_prior:
        drift_basis = "episodes"
        drift_ratio, drift_status = _drift(episodes_7d, episodes_prior)
    else:
        drift_basis = "signals_fallback_incomplete_episode_baseline"
        drift_ratio, drift_status = raw_signal_drift_ratio, raw_signal_drift_status
    matured = int((counts or {}).get("matured_30m_7d") or 0)
    trading_days = int((counts or {}).get("matured_days_7d") or 0)
    validation_matured = int((counts or {}).get("matured_30m_total") or 0)
    validation_days = int((counts or {}).get("matured_days_total") or 0)
    gate_status = (
        "ready_for_formal_validation"
        if validation_matured >= FORMAL_VALIDATION_MIN_MATURED_SIGNALS
        and validation_days >= FORMAL_VALIDATION_MIN_TRADING_DAYS
        else "accumulating"
    )
    quote_age = _age_seconds((latest_quotes or {}).get("latest_quote_at"), now)
    market_session = market_session_projection(now, (latest_quotes or {}).get("calendar_is_open"))
    quote_status = (
        "expected_idle" if not market_session["quote_required"]
        else "fresh" if quote_age is not None and quote_age <= 90
        else "stale_or_missing"
    )
    rows = int((outcomes or {}).get("rows") or 0)
    positive = int((outcomes or {}).get("positive") or 0)
    recommendation = health_recommendation(
        drift_status=drift_status, quote_status=quote_status, gate_status=gate_status,
        matured=validation_matured, trading_days=validation_days,
    )
    return {
        "status": "research_only",
        "observed_at": now,
        "window": {"current_days": HEALTH_WINDOW_DAYS, "baseline_days": HEALTH_WINDOW_DAYS},
        "trigger_frequency": {
            "signals_7d": signals_7d, "signals_prior_7d": signals_prior,
            "episodes_7d": episodes_7d, "episodes_prior_7d": episodes_prior,
            "drift_ratio": round(drift_ratio, 4) if drift_ratio is not None else None,
            "drift_status": drift_status, "drift_basis": drift_basis,
            "raw_signal_drift_ratio": round(raw_signal_drift_ratio, 4) if raw_signal_drift_ratio is not None else None,
            "raw_signal_drift_status": raw_signal_drift_status,
        },
        "outcomes_30m": {
            "matured": matured, "trading_days": trading_days, "rows": rows,
            "window_days": HEALTH_WINDOW_DAYS, "anchor": "signal_observed_at",
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
        "market_session": market_session,
        "strategy_breakdown": strategy_family_breakdown(strategy_rows),
        "validation_gate": {
            "status": gate_status,
            "observed_matured_signals": validation_matured,
            "observed_trading_days": validation_days,
            "required_matured_signals": FORMAL_VALIDATION_MIN_MATURED_SIGNALS,
            "required_trading_days": FORMAL_VALIDATION_MIN_TRADING_DAYS,
            "evidence_window": "lifetime_matured_30m_events",
            "live_effect": "none",
        },
        "governance_recommendation": recommendation,
        "notice": "健康/漂移只用于研究监控；不会在线调参、晋级或改变分析师权重。",
    }


__all__ = [
    "health_recommendation", "latest_strategy_health", "market_session_projection",
    "strategy_family_breakdown", "strategy_health_payload_from_rows",
]
