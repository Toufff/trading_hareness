"""Local health-payload assembly for the loopback research service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


class DatabaseUnavailableError(RuntimeError):
    """The synchronous health probe could not reach the local repository."""


@dataclass(frozen=True)
class HealthDependencies:
    database: Any
    post_close_lease_key: str
    background_loop_lease_seconds: Callable[[], int]
    data_directory: Callable[[], Path]
    resource_status: Callable[[Path], dict[str, Any]]
    public_http_client_status: Callable[[], dict[str, Any]]
    alert_http_client_status: Callable[[], dict[str, Any]]
    provider_http_client_status: Callable[[], dict[str, Any]]
    provider_request_reservation_status: Callable[[], dict[str, Any]]
    runtime_executor_status: Callable[[], dict[str, Any]]
    super_get_executor_status: Callable[[], dict[str, Any]]
    provider_status: Callable[[], list[dict[str, Any]]]
    free_provider_status: Callable[[], list[dict[str, Any]]]
    realtime_market_session: Callable[[], tuple[bool, str]]
    board_curve_session: Callable[[], tuple[bool, str]]
    scan_interval_seconds: Callable[[], int]
    effective_scan_interval_seconds: Callable[[int, datetime], int]
    high_frequency_window: Callable[[datetime], bool]
    super_get_fast_interval_seconds: Callable[[], float]
    super_get_fast_max_in_flight: Callable[[], int]
    fast_quote_retention_days: Callable[[], int]
    board_curve_enabled: Callable[[], bool]
    board_curve_retention_days: Callable[[], int]
    board_rotation_retention_days: Callable[[], int]
    set_db_pool_gauge: Callable[[dict[str, Any]], None]
    set_open_circuit_gauge: Callable[[int], None]


def health_payload(deps: HealthDependencies) -> dict[str, Any]:
    """Build health evidence from local state only; no market request occurs."""
    try:
        deps.database.ping()
    except Exception as error:  # noqa: BLE001 - endpoint translates to an HTTP health failure
        raise DatabaseUnavailableError(str(error)) from error
    local_now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    session_active, session_reason = deps.realtime_market_session()
    board_session_active, board_session_reason = deps.board_curve_session()
    normal_interval = deps.scan_interval_seconds()
    loop_lease_seconds = deps.background_loop_lease_seconds()
    pool = deps.database.pool_status()
    deps.set_db_pool_gauge(pool)
    with deps.database.transaction() as connection:
        open_circuits = connection.execute(
            "SELECT count(*)::int AS count FROM quant.provider_health WHERE circuit_open_until > now()"
        ).fetchone()["count"]
        post_close_lease = connection.execute(
            """SELECT expires_at,updated_at FROM quant.runtime_leases
                 WHERE lease_key=%s AND expires_at > now()""",
            (deps.post_close_lease_key,),
        ).fetchone()
        background_loop_leases = connection.execute(
            """SELECT lease_key,expires_at,updated_at FROM quant.runtime_leases
                 WHERE lease_key LIKE 'background_loop:%' AND expires_at > now()
                 ORDER BY lease_key"""
        ).fetchall()
    deps.set_open_circuit_gauge(int(open_circuits))
    return {
        "status": "ok", "service": "quant-research", "database_pool": pool,
        "resources": deps.resource_status(deps.data_directory()),
        "runtime_leases": {
            "background_loop_lease_seconds": loop_lease_seconds,
            "post_close_refresh": {
                "active": bool(post_close_lease),
                "expires_at": post_close_lease["expires_at"] if post_close_lease else None,
                "updated_at": post_close_lease["updated_at"] if post_close_lease else None,
            },
            "background_loops": [dict(row) for row in background_loop_leases],
        },
        "http_clients": {
            "public_market": deps.public_http_client_status(), "feishu_alert": deps.alert_http_client_status(),
            "tushare_provider": deps.provider_http_client_status(),
        },
        "provider_rate_limits": deps.provider_request_reservation_status(),
        "blocking_executors": {**deps.runtime_executor_status(), "super_get": deps.super_get_executor_status()},
        "market_providers": [*deps.provider_status(), *deps.free_provider_status()],
        "intraday_automation": {
            "enabled": normal_interval >= 30, "normal_scan_interval_seconds": normal_interval,
            "effective_scan_interval_seconds": deps.effective_scan_interval_seconds(normal_interval, local_now),
            "special_window_active": deps.high_frequency_window(local_now), "special_window_scan_interval_seconds": 10,
            "super_get_fast_interval_seconds": deps.super_get_fast_interval_seconds(),
            "super_get_fast_max_in_flight": deps.super_get_fast_max_in_flight(),
            "fast_quote_retention_days": deps.fast_quote_retention_days(), "board_curve_enabled": deps.board_curve_enabled(),
            "board_curve_interval_seconds": 60, "board_curve_retention_days": deps.board_curve_retention_days(),
            "board_rotation_retention_days": deps.board_rotation_retention_days(),
            "board_curve_session_active": board_session_active, "board_curve_session_reason": board_session_reason,
            "session_active": session_active, "session_reason": session_reason, "timezone": "Asia/Shanghai",
        },
    }


__all__ = ["DatabaseUnavailableError", "HealthDependencies", "health_payload"]
