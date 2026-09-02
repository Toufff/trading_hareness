"""Local health-payload assembly for the loopback research service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    remote_archive_http_client_status: Callable[[], dict[str, Any]]
    network_status: Callable[[], dict[str, Any]]
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
    async_database_pool_status: Callable[[], dict[str, Any]] | None = None
    research_storage_governance: Callable[[Any], dict[str, Any]] | None = None
    background_loop_status: Callable[[], dict[str, dict[str, Any]]] | None = None
    runtime_task_contracts: Callable[[], list[dict[str, Any]]] | None = None
    optional_background_tasks: Callable[[], dict[str, bool]] | None = None
    daily_control_plane_status: Callable[[], dict[str, Any]] | None = None
    live_session_acceptance_status: Callable[[], dict[str, Any]] | None = None
    release_metadata: Callable[[], dict[str, str | None]] | None = None
    post_close_runtime_status: Callable[[], dict[str, Any]] | None = None
    control_plane_writes_enabled: Callable[[], bool] | None = None


def runtime_loops_with_lease_heartbeats(
    runtime_loops: dict[str, dict[str, Any]], background_loop_leases: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Attach durable lease renewal evidence without relabelling lifecycle state.

    ``updated_at`` is deliberately the last lifecycle transition.  A long
    lived loop therefore needs a separate heartbeat field: lease renewal is a
    local, cross-process proof that its current owner is still active.
    """
    leases = {
        str(row.get("lease_key") or "").removeprefix("background_loop:"): row
        for row in background_loop_leases
    }
    result: dict[str, dict[str, Any]] = {}
    for label, item in runtime_loops.items():
        lease = leases.get(label)
        result[label] = {
            **dict(item),
            "lease_heartbeat_at": lease.get("updated_at") if lease else None,
            "lease_expires_at": lease.get("expires_at") if lease else None,
        }
    return result


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
    resources = deps.resource_status(deps.data_directory())
    if deps.research_storage_governance is not None:
        resources["research_storage"] = deps.research_storage_governance(deps.database)
    background_leases = [dict(row) for row in background_loop_leases]
    runtime_loops = deps.background_loop_status() if deps.background_loop_status else {}
    return {
        "status": "ok", "service": "quant-research", "database_pool": pool,
        "control_plane_writes_enabled": deps.control_plane_writes_enabled() if deps.control_plane_writes_enabled else True,
        "build": deps.release_metadata() if deps.release_metadata else {},
        "async_database_pool": deps.async_database_pool_status() if deps.async_database_pool_status else None,
        "resources": resources,
        "runtime_leases": {
            "background_loop_lease_seconds": loop_lease_seconds,
            "post_close_refresh": {
                "active": bool(post_close_lease),
                "expires_at": post_close_lease["expires_at"] if post_close_lease else None,
                "updated_at": post_close_lease["updated_at"] if post_close_lease else None,
            },
            "background_loops": background_leases,
        },
        "runtime_loops": runtime_loops_with_lease_heartbeats(runtime_loops, background_leases),
        "runtime_tasks": {
            "post_close_refresh": deps.post_close_runtime_status() if deps.post_close_runtime_status else {},
        },
        "runtime_task_contracts": deps.runtime_task_contracts() if deps.runtime_task_contracts else [],
        "optional_background_tasks": deps.optional_background_tasks() if deps.optional_background_tasks else {},
        "daily_control_plane": deps.daily_control_plane_status() if deps.daily_control_plane_status else {},
        "live_session_acceptance": deps.live_session_acceptance_status() if deps.live_session_acceptance_status else {},
        "http_clients": {
            "public_market": deps.public_http_client_status(), "feishu_alert": deps.alert_http_client_status(),
            "tushare_provider": deps.provider_http_client_status(),
            "remote_analyst_archive": deps.remote_archive_http_client_status(),
        },
        "network": deps.network_status(),
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


__all__ = [
    "DatabaseUnavailableError", "HealthDependencies", "health_payload",
    "runtime_loops_with_lease_heartbeats",
]
