"""Read-only gates for future historical replay and strategy validation.

The service deliberately separates a local readiness check from any backfill
job: inspecting these gates must never create provider requests or downloads.
"""

from __future__ import annotations

from typing import Any, Mapping


P2_MIN_FULL_CROSS_SECTION_DAYS = 3 * 244
P3_MIN_REPLAY_DAYS = 60
P3_MIN_SIGNAL_EVENTS = 200


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
            **dict(metrics), "offline_minute_symbols": minute_symbols,
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
    }


def historical_replay_readiness(database: Any) -> dict[str, Any]:
    """Read bounded local coverage metrics for P2/P3 admission."""
    with database.transaction() as connection:
        row = connection.execute(
            """WITH universe AS (
                    SELECT greatest(1,count(*)::int) symbols
                      FROM quant.universe_members
                     WHERE universe_key='all_a' AND enabled
                 ), daily_counts AS (
                    SELECT trading_date,count(DISTINCT symbol)::int symbols
                      FROM quant.canonical_bars_daily
                     WHERE symbol<>'000300.SH'
                     GROUP BY trading_date
                 )
                SELECT
                  (SELECT min(trading_date) FROM daily_counts) first_daily_date,
                  (SELECT max(trading_date) FROM daily_counts) latest_daily_date,
                  (SELECT count(*)::int FROM daily_counts d,universe u
                    WHERE d.symbols>=least(u.symbols*0.8,1000)) full_cross_section_days,
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
    "P2_MIN_FULL_CROSS_SECTION_DAYS", "P3_MIN_REPLAY_DAYS", "P3_MIN_SIGNAL_EVENTS",
    "historical_replay_readiness", "replay_readiness_payload",
]
