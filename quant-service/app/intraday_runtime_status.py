"""Read-only persistence boundary for the intraday runtime status panel.

This module deliberately contains no provider client and no scheduler logic.
The dashboard can inspect a degraded decision path without causing market-data
traffic or mutating alert delivery state.
"""

from __future__ import annotations

from typing import Any


def load_intraday_runtime_evidence(database: Any, max_alert_attempts: int) -> dict[str, Any]:
    """Load the bounded evidence set used by the runtime health endpoint."""
    with database.transaction() as connection:
        health_rows = connection.execute(
            """SELECT provider_key,capability,consecutive_failures,circuit_open_until,last_success_at,
                      last_failure_at,last_error,last_latency_ms,last_row_count,updated_at
                 FROM quant.provider_health
                WHERE provider_key IN ('tencent_free','tushare_super_get','tushare_super')
                  AND capability IN ('realtime_quote','order_book_quote','rt_k','rt_min','rt_min_daily')"""
        ).fetchall()
        quote_rows = connection.execute(
            """SELECT source_name,max(observed_at) AS last_observed_at,count(*)::int AS rows
                 FROM quant.intraday_quote_observations
                WHERE source_name IN ('tencent_free','tencent_order_book','tushare_super_get_rt_k') GROUP BY source_name"""
        ).fetchall()
        raw_rows = connection.execute(
            """SELECT api_name,max(available_at) AS last_observed_at,count(*)::int AS rows
                 FROM quant.tushare_raw_records
                WHERE api_name IN ('rt_k','rt_min','rt_min_daily') GROUP BY api_name"""
        ).fetchall()
        minute_profile = connection.execute(
            """SELECT max(available_at) AS last_observed_at,count(*)::int AS rows,
                      max(trading_date) AS latest_trading_date
                 FROM quant.intraday_minute_sessions
                WHERE source_name='tencent_intraday_minutes'"""
        ).fetchone()
        latest_scan = connection.execute(
            "SELECT status,observed_at,source_status,summary FROM quant.intraday_scan_runs ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        latest_completed_scan = connection.execute(
            "SELECT status,observed_at,source_status,summary FROM quant.intraday_scan_runs WHERE status='completed' ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        latest_board = connection.execute(
            "SELECT status,observed_at,source_status,summary FROM quant.intraday_board_reports ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        latest_board_curve = connection.execute(
            """SELECT status,observed_at,source_status,coverage
                 FROM quant.intraday_board_flow_snapshots ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
        latest_delivery = connection.execute(
            """SELECT 'signal' AS kind,status,sent_at,created_at,error_message
                 FROM quant.intraday_alert_deliveries
                ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        delivery_history = connection.execute(
            """SELECT status FROM quant.intraday_alert_deliveries
                ORDER BY created_at DESC LIMIT 20"""
        ).fetchall()
        pending_delivery_count = connection.execute(
            """SELECT count(*)::int AS count FROM quant.intraday_alert_deliveries
                 WHERE channel='feishu_adapter' AND status IN ('pending','failed')
                   AND message_text IS NOT NULL AND message_text<>''
                   AND attempt_count<%s""",
            (max_alert_attempts,),
        ).fetchone()["count"]
        pending_rotation_delivery_count = connection.execute(
            """SELECT count(*)::int AS count FROM quant.intraday_board_rotation_deliveries
                 WHERE channel='feishu_adapter' AND status IN ('pending','failed')
                   AND attempt_count<%s""",
            (max_alert_attempts,),
        ).fetchone()["count"]
        latest_daily_summary = connection.execute(
            """SELECT exchange_date,delivery_status,attempt_count,next_attempt_at,sent_at,error_message,updated_at
                 FROM quant.strategy_day_summaries
                ORDER BY exchange_date DESC LIMIT 1"""
        ).fetchone()
        latest_health_event = connection.execute(
            """SELECT event_type,streak_count,delivery_status,error_message,created_at,updated_at
                 FROM quant.alert_delivery_health_events
                WHERE channel='feishu_adapter'
                ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        watch_row = connection.execute(
            "SELECT count(*)::int AS enabled FROM quant.intraday_watchlists WHERE enabled"
        ).fetchone()
    return {
        "health_rows": health_rows, "quote_rows": quote_rows, "raw_rows": raw_rows, "minute_profile": minute_profile,
        "latest_scan": latest_scan, "latest_completed_scan": latest_completed_scan,
        "latest_board": latest_board, "latest_board_curve": latest_board_curve,
        "latest_delivery": latest_delivery, "delivery_history": delivery_history,
        "pending_delivery_count": pending_delivery_count, "pending_rotation_delivery_count": pending_rotation_delivery_count,
        "latest_daily_summary": latest_daily_summary,
        "latest_health_event": latest_health_event, "watch_row": watch_row,
    }


async def load_intraday_runtime_evidence_async(async_database: Any, max_alert_attempts: int) -> dict[str, Any]:
    """Native async equivalent used by the frontend status route.

    This is deliberately a read-only projection.  It does not call a market
    provider, mutate delivery state, or acquire a scheduler lease.
    """
    async with async_database.transaction() as connection:
        async def all_rows(sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
            result = await connection.execute(sql, params)
            return await result.fetchall()

        async def one_row(sql: str, params: tuple[Any, ...] = ()) -> Any:
            result = await connection.execute(sql, params)
            return await result.fetchone()

        health_rows = await all_rows(
            """SELECT provider_key,capability,consecutive_failures,circuit_open_until,last_success_at,
                      last_failure_at,last_error,last_latency_ms,last_row_count,updated_at
                 FROM quant.provider_health
                WHERE provider_key IN ('tencent_free','tushare_super_get','tushare_super')
                  AND capability IN ('realtime_quote','order_book_quote','rt_k','rt_min','rt_min_daily')"""
        )
        quote_rows = await all_rows(
            """SELECT source_name,max(observed_at) AS last_observed_at,count(*)::int AS rows
                 FROM quant.intraday_quote_observations
                WHERE source_name IN ('tencent_free','tencent_order_book','tushare_super_get_rt_k') GROUP BY source_name"""
        )
        raw_rows = await all_rows(
            """SELECT api_name,max(available_at) AS last_observed_at,count(*)::int AS rows
                 FROM quant.tushare_raw_records
                WHERE api_name IN ('rt_k','rt_min','rt_min_daily') GROUP BY api_name"""
        )
        minute_profile = await one_row(
            """SELECT max(available_at) AS last_observed_at,count(*)::int AS rows,
                      max(trading_date) AS latest_trading_date
                 FROM quant.intraday_minute_sessions
                WHERE source_name='tencent_intraday_minutes'"""
        )
        latest_scan = await one_row("SELECT status,observed_at,source_status,summary FROM quant.intraday_scan_runs ORDER BY observed_at DESC LIMIT 1")
        latest_completed_scan = await one_row("SELECT status,observed_at,source_status,summary FROM quant.intraday_scan_runs WHERE status='completed' ORDER BY observed_at DESC LIMIT 1")
        latest_board = await one_row("SELECT status,observed_at,source_status,summary FROM quant.intraday_board_reports ORDER BY observed_at DESC LIMIT 1")
        latest_board_curve = await one_row(
            """SELECT status,observed_at,source_status,coverage
                 FROM quant.intraday_board_flow_snapshots ORDER BY observed_at DESC LIMIT 1"""
        )
        latest_delivery = await one_row(
            """SELECT 'signal' AS kind,status,sent_at,created_at,error_message
                 FROM quant.intraday_alert_deliveries ORDER BY created_at DESC LIMIT 1"""
        )
        delivery_history = await all_rows(
            "SELECT status FROM quant.intraday_alert_deliveries ORDER BY created_at DESC LIMIT 20"
        )
        pending_delivery = await one_row(
            """SELECT count(*)::int AS count FROM quant.intraday_alert_deliveries
                 WHERE channel='feishu_adapter' AND status IN ('pending','failed')
                   AND message_text IS NOT NULL AND message_text<>'' AND attempt_count<%s""",
            (max_alert_attempts,),
        )
        pending_rotation = await one_row(
            """SELECT count(*)::int AS count FROM quant.intraday_board_rotation_deliveries
                 WHERE channel='feishu_adapter' AND status IN ('pending','failed') AND attempt_count<%s""",
            (max_alert_attempts,),
        )
        latest_daily_summary = await one_row(
            """SELECT exchange_date,delivery_status,attempt_count,next_attempt_at,sent_at,error_message,updated_at
                 FROM quant.strategy_day_summaries ORDER BY exchange_date DESC LIMIT 1"""
        )
        latest_health_event = await one_row(
            """SELECT event_type,streak_count,delivery_status,error_message,created_at,updated_at
                 FROM quant.alert_delivery_health_events WHERE channel='feishu_adapter'
                 ORDER BY created_at DESC LIMIT 1"""
        )
        watch_row = await one_row("SELECT count(*)::int AS enabled FROM quant.intraday_watchlists WHERE enabled")
    return {
        "health_rows": health_rows, "quote_rows": quote_rows, "raw_rows": raw_rows, "minute_profile": minute_profile,
        "latest_scan": latest_scan, "latest_completed_scan": latest_completed_scan,
        "latest_board": latest_board, "latest_board_curve": latest_board_curve,
        "latest_delivery": latest_delivery, "delivery_history": delivery_history,
        "pending_delivery_count": int((pending_delivery or {}).get("count") or 0),
        "pending_rotation_delivery_count": int((pending_rotation or {}).get("count") or 0),
        "latest_daily_summary": latest_daily_summary, "latest_health_event": latest_health_event, "watch_row": watch_row,
    }


__all__ = ["load_intraday_runtime_evidence", "load_intraday_runtime_evidence_async"]
