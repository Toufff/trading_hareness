"""Provider health presentation without polling or mutating provider state."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping


def provider_health_item(
    row: Mapping[str, Any],
    *,
    configured: bool | None,
    observed_at: datetime,
) -> dict[str, Any]:
    """Turn stored health evidence into an explicit front-end state.

    This intentionally does not infer that a configured credential is healthy:
    success must be observed, while a live circuit or newer failure is visible.
    """
    item = dict(row)
    circuit_open_until = item.get("circuit_open_until")
    last_success_at = item.get("last_success_at")
    last_failure_at = item.get("last_failure_at")
    enabled = bool(item.get("enabled"))
    if not enabled:
        state, reason = "disabled", "provider capability is disabled in the control plane"
    elif configured is False:
        state, reason = "unconfigured", "provider credentials or runtime dependency are not configured"
    elif circuit_open_until is not None and circuit_open_until > observed_at:
        state, reason = "circuit_open", "three or more consecutive failures opened the temporary circuit"
    elif last_failure_at is not None and (last_success_at is None or last_failure_at >= last_success_at):
        state, reason = "degraded", "latest recorded provider operation failed"
    elif last_success_at is not None:
        state, reason = "healthy", "latest recorded provider operation completed successfully"
    else:
        state, reason = "unknown", "no recorded provider operation for this capability"
    item.update({"configured": configured, "state": state, "state_reason": reason})
    return item


def provider_health_summary(items: list[Mapping[str, Any]]) -> dict[str, int]:
    states = ("healthy", "degraded", "circuit_open", "unconfigured", "disabled", "unknown")
    return {state: sum(str(item.get("state")) == state for item in items) for state in states}


def provider_health_snapshot(database: Any, provider_configs: list[Mapping[str, Any]],
                             observed_at: datetime) -> dict[str, Any]:
    """Read the bounded provider-health view without polling any upstream."""
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT p.provider_key,p.label,p.enabled,c.capability,c.market,c.priority,c.rate_limit_per_minute,
                      h.consecutive_failures,h.circuit_open_until,h.last_success_at,h.last_failure_at,h.last_error,h.last_latency_ms,h.last_row_count
               FROM quant.providers p LEFT JOIN quant.provider_capabilities c ON c.provider_key=p.provider_key
               LEFT JOIN quant.provider_health h ON h.provider_key=c.provider_key AND h.capability=c.capability AND h.market=c.market
               ORDER BY c.capability,p.provider_key"""
        ).fetchall()
    return project_provider_health([dict(row) for row in rows], provider_configs, observed_at)


def project_provider_health(rows: list[Mapping[str, Any]], provider_configs: list[Mapping[str, Any]],
                            observed_at: datetime) -> dict[str, Any]:
    configured = {str(item["provider_key"]): bool(item.get("configured")) for item in provider_configs}
    items = [provider_health_item(row, configured=configured.get(str(row["provider_key"])), observed_at=observed_at) for row in rows]
    return {"observed_at": observed_at, "summary": provider_health_summary(items), "items": items}
