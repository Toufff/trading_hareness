"""Read-only gates for future historical replay and strategy validation.

The service deliberately separates a local readiness check from any backfill
job: inspecting these gates must never create provider requests or downloads.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping


# A-share calendars normally have about 240--244 trading days in a year.
# Requiring 3 * 244 made a correctly collected three-calendar-year dataset
# impossible to admit in a normal year.  Keep both a conservative trading-day
# floor and a calendar-span floor, rather than silently lowering the evidence
# requirement to an arbitrary row count.
P2_MIN_FULL_CROSS_SECTION_DAYS = 720
P2_MIN_DAILY_CALENDAR_SPAN_DAYS = 1090
P3_MIN_REPLAY_DAYS = 60
P3_MIN_SIGNAL_EVENTS = 200


# A historical "full cross-section" is relative to the membership that was
# eligible on that trading date, not to today's live universe.  It also needs
# the daily control-plane records that the replay will consume.  Keep this SQL
# fragment shared with the native-async read projection so both paths publish
# the same point-in-time evidence.
PIT_DAILY_COVERAGE_CTE = """WITH daily_dates AS (
        SELECT DISTINCT trading_date
          FROM quant.canonical_bars_daily
         WHERE symbol<>'000300.SH'
    ), expected_universe AS (
        SELECT dates.trading_date,count(DISTINCT membership.symbol)::int AS expected_symbols
          FROM daily_dates dates
          JOIN quant.universe_membership_history membership
            ON membership.universe_key='all_a'
           AND membership.effective_from<=dates.trading_date
           AND (membership.effective_to IS NULL OR membership.effective_to>=dates.trading_date)
         GROUP BY dates.trading_date
    ), daily_controls AS (
        SELECT bars.trading_date,
               count(DISTINCT bars.symbol)::int AS bar_symbols,
               count(DISTINCT bars.symbol) FILTER (WHERE fundamentals.symbol IS NOT NULL)::int AS fundamental_symbols,
               count(DISTINCT bars.symbol) FILTER (WHERE limits.symbol IS NOT NULL)::int AS limit_symbols
          FROM quant.canonical_bars_daily bars
          LEFT JOIN quant.daily_fundamentals fundamentals
            ON fundamentals.symbol=bars.symbol AND fundamentals.trading_date=bars.trading_date
          LEFT JOIN quant.daily_trade_limits limits
            ON limits.symbol=bars.symbol AND limits.trading_date=bars.trading_date
         WHERE bars.symbol<>'000300.SH'
         GROUP BY bars.trading_date
    ), full_dates AS (
        SELECT controls.trading_date,controls.bar_symbols,controls.fundamental_symbols,
               controls.limit_symbols,universe.expected_symbols
          FROM daily_controls controls
          JOIN expected_universe universe USING(trading_date)
         WHERE universe.expected_symbols>=1000
           AND controls.bar_symbols>=greatest(ceil(universe.expected_symbols*0.8)::int,1000)
           AND controls.fundamental_symbols>=greatest(ceil(universe.expected_symbols*0.8)::int,1000)
           AND controls.limit_symbols>=greatest(ceil(universe.expected_symbols*0.8)::int,1000)
    )"""


def replay_readiness_payload(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Turn local evidence counts into explicit P2/P3 gates."""
    full_days = int(metrics.get("full_cross_section_days") or 0)
    minute_days = int(metrics.get("offline_minute_trading_days") or 0)
    minute_symbols = int(metrics.get("offline_minute_symbols") or 0)
    minute_bars = int(metrics.get("offline_minute_bars") or 0)
    minute_clock_bars = int(metrics.get("offline_minute_source_clock_bars") or 0)
    minute_clock_days = int(metrics.get("offline_minute_source_clock_days") or 0)
    forward_rule_input_days = int(metrics.get("forward_rule_input_days") or 0)
    forward_rule_input_rows = int(metrics.get("forward_rule_input_rows") or 0)
    imports = int(metrics.get("completed_offline_imports") or 0)
    confirmed_signals = int(metrics.get("confirmed_signal_events") or 0)
    matured_signals = int(metrics.get("matured_signal_events") or 0)
    first_full_date = _as_date(metrics.get("first_full_cross_section_date"))
    latest_full_date = _as_date(metrics.get("latest_full_cross_section_date"))
    calendar_span_days = (
        max(0, (latest_full_date - first_full_date).days)
        if first_full_date and latest_full_date else 0
    )

    def gate(key: str, stage: str, observed: int, required: int, unit: str, notice: str) -> dict[str, Any]:
        return {
            "key": key, "stage": stage, "observed": observed, "required": required, "unit": unit,
            "status": "ready" if observed >= required else "insufficient", "notice": notice,
        }

    gates = [
        gate(
            "p2_daily_point_in_time_history", "P2", full_days, P2_MIN_FULL_CROSS_SECTION_DAYS, "full_cross_section_days",
            "Requires delisting-aware, point-in-time daily/control-plane history before factor or replay claims.",
        ),
        gate(
            "p2_daily_calendar_span", "P2", calendar_span_days, P2_MIN_DAILY_CALENDAR_SPAN_DAYS,
            "calendar_days_between_first_and_latest_full_cross_section",
            "The full cross-sections must span roughly three calendar years; a dense but short backfill cannot satisfy P2.",
        ),
        gate(
            "p2_offline_minute_source", "P2", imports, 1, "completed_offline_imports",
            "Historical minute data is accepted only through the mounted offline import path; this check never fetches it.",
        ),
        gate(
            "p2_offline_minute_availability_clock", "P2", minute_clock_bars, 1, "bars_with_source_available_at",
            "Minute replay needs a vendor-recorded source_available_at; local import time and bar-close time are not substitutes.",
        ),
        gate(
            "p3_replay_window", "P3", min(full_days, minute_days), P3_MIN_REPLAY_DAYS, "aligned_daily_and_minute_days",
            "Strategy replay requires at least 60 locally available daily and offline-minute trading days before threshold calibration.",
        ),
        gate(
            "p3_signal_sample", "P3", confirmed_signals, P3_MIN_SIGNAL_EVENTS, "confirmed_signal_events",
            "Promotion requires an independent replay/observation sample; detected-only events do not count.",
        ),
    ]
    p2_ready = all(item["status"] == "ready" for item in gates if item["stage"] == "P2")
    p3_ready = p2_ready and all(item["status"] == "ready" for item in gates if item["stage"] == "P3")
    return {
        "status": "ready" if p3_ready else "blocked",
        "p2_data_foundation_ready": p2_ready,
        "p3_strategy_validation_ready": p3_ready,
        "gates": gates,
        "evidence": {
            **dict(metrics), "first_full_cross_section_date": str(first_full_date) if first_full_date else None,
            "latest_full_cross_section_date": str(latest_full_date) if latest_full_date else None,
            "full_cross_section_calendar_span_days": calendar_span_days,
            "offline_minute_symbols": minute_symbols,
            "offline_minute_bars": minute_bars, "offline_minute_source_clock_bars": minute_clock_bars,
            "offline_minute_source_clock_days": minute_clock_days,
            "forward_rule_input_days": forward_rule_input_days,
            "forward_rule_input_rows": forward_rule_input_rows, "matured_signal_events": matured_signals,
        },
        "forward_capture": {
            "status": "ready" if forward_rule_input_days >= P3_MIN_REPLAY_DAYS else "accumulating",
            "observed_days": forward_rule_input_days, "observed_rows": forward_rule_input_rows,
            "required_days": P3_MIN_REPLAY_DAYS,
            "notice": (
                "Forward-only core-rule reproducibility evidence. It is reported separately and does not "
                "substitute for the point-in-time historical minute/replay gates."
            ),
        },
        "policy": "Read-only local evidence check: it does not call providers, download history, or change strategy thresholds.",
        "coverage_definition": "point_in_time_all_a_membership_with_daily_bars_fundamentals_and_trade_limits_at_80pct_min_1000",
    }


def _as_date(value: Any) -> date | None:
    """Accept database date/datetime values without trusting a local timezone."""
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def historical_replay_readiness(database: Any) -> dict[str, Any]:
    """Read bounded local coverage metrics for P2/P3 admission."""
    with database.transaction() as connection:
        row = connection.execute(
            f"""{PIT_DAILY_COVERAGE_CTE}
                SELECT
                  (SELECT min(trading_date) FROM daily_dates) first_daily_date,
                  (SELECT max(trading_date) FROM daily_dates) latest_daily_date,
                  (SELECT min(trading_date) FROM full_dates) first_full_cross_section_date,
                  (SELECT max(trading_date) FROM full_dates) latest_full_cross_section_date,
                  (SELECT count(*)::int FROM full_dates) full_cross_section_days,
                  (SELECT count(*)::int FROM daily_dates) daily_bar_days,
                  (SELECT count(*)::int FROM expected_universe) point_in_time_universe_days,
                  (SELECT count(*)::int FROM daily_dates dates
                    WHERE NOT EXISTS (SELECT 1 FROM expected_universe universe
                                      WHERE universe.trading_date=dates.trading_date)) missing_point_in_time_universe_days,
                  (SELECT count(DISTINCT (bar_time AT TIME ZONE 'Asia/Shanghai')::date)::int
                     FROM quant.market_bars_minute) offline_minute_trading_days,
                  (SELECT count(DISTINCT symbol)::int FROM quant.market_bars_minute) offline_minute_symbols,
                  (SELECT count(*)::int FROM quant.market_bars_minute) offline_minute_bars,
                  (SELECT count(*)::int FROM quant.market_bars_minute WHERE source_available_at IS NOT NULL) offline_minute_source_clock_bars,
                  (SELECT count(DISTINCT (source_available_at AT TIME ZONE 'Asia/Shanghai')::date)::int
                     FROM quant.market_bars_minute WHERE source_available_at IS NOT NULL) offline_minute_source_clock_days,
                  (SELECT count(DISTINCT (observed_at AT TIME ZONE 'Asia/Shanghai')::date)::int
                     FROM quant.intraday_rule_input_snapshots) forward_rule_input_days,
                  (SELECT count(*)::int FROM quant.intraday_rule_input_snapshots) forward_rule_input_rows,
                  (SELECT count(*)::int FROM quant.offline_imports WHERE status IN ('completed','partial')) completed_offline_imports,
                  (SELECT count(*)::int FROM quant.intraday_signal_events
                    WHERE state IN ('confirmed','alerted')) confirmed_signal_events,
                  (SELECT count(DISTINCT signal_event_id)::int FROM quant.intraday_signal_outcomes
                    WHERE status='matured') matured_signal_events"""
        ).fetchone()
    return replay_readiness_payload(dict(row or {}))


__all__ = [
    "P2_MIN_FULL_CROSS_SECTION_DAYS", "P2_MIN_DAILY_CALENDAR_SPAN_DAYS", "P3_MIN_REPLAY_DAYS", "P3_MIN_SIGNAL_EVENTS",
    "PIT_DAILY_COVERAGE_CTE", "historical_replay_readiness", "replay_readiness_payload",
]
