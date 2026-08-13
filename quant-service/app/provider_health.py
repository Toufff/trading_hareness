"""Persistence and classification for provider-health evidence."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from .capability_registry import api_capability, catalog_metadata
from .telemetry import provider_latency_seconds, provider_requests_total
from .tushare_providers import safe_error_detail


def record_provider_success(connection: Any, provider: str, capability: str, rows: int,
                            latency_ms: int | None = None) -> None:
    provider_requests_total.labels(provider, capability, "success").inc()
    if latency_ms is not None and latency_ms >= 0:
        provider_latency_seconds.labels(provider, capability).observe(latency_ms / 1000)
    connection.execute(
        """INSERT INTO quant.provider_health(provider_key,capability,market,consecutive_failures,last_success_at,last_latency_ms,last_row_count)
           VALUES(%s,%s,'cn',0,now(),%s,%s)
           ON CONFLICT(provider_key,capability,market) DO UPDATE SET consecutive_failures=0,circuit_open_until=null,
             last_success_at=now(),last_error=null,last_latency_ms=EXCLUDED.last_latency_ms,last_row_count=EXCLUDED.last_row_count,updated_at=now()""",
        (provider, capability, latency_ms, rows),
    )


def record_provider_failure(connection: Any, provider: str, capability: str, error: str,
                           latency_ms: int | None = None) -> None:
    provider_requests_total.labels(provider, capability, "failure").inc()
    if latency_ms is not None and latency_ms >= 0:
        provider_latency_seconds.labels(provider, capability).observe(latency_ms / 1000)
    connection.execute(
        """INSERT INTO quant.provider_health(provider_key,capability,market,consecutive_failures,last_failure_at,last_error,last_latency_ms,circuit_open_until)
           VALUES(%s,%s,'cn',1,now(),%s,%s,null)
           ON CONFLICT(provider_key,capability,market) DO UPDATE SET consecutive_failures=quant.provider_health.consecutive_failures+1,
             last_failure_at=now(),last_error=EXCLUDED.last_error,
             last_latency_ms=COALESCE(EXCLUDED.last_latency_ms,quant.provider_health.last_latency_ms),
             circuit_open_until=CASE WHEN quant.provider_health.consecutive_failures+1 >= 3 THEN now()+interval '5 minutes' ELSE null END,
             updated_at=now()""",
        (provider, capability, safe_error_detail(error, 500), latency_ms),
    )


def record_provider_api_capability(connection: Any, provider: str, api_name: str, availability: str,
                                   rows: int | None = None, note: str = "") -> None:
    """Keep permission/probe evidence distinct from generic request health."""
    contract = api_capability(api_name)
    metadata = {**catalog_metadata(api_name), "last_observation": availability,
                **({"last_row_count": rows} if rows is not None else {})}
    connection.execute(
        """INSERT INTO quant.provider_api_capabilities(provider_key,api_name,availability,frequency,decision_eligible,note,verified_at,last_checked_at,metadata)
           VALUES(%s,%s,%s,%s,%s,%s,CASE WHEN %s='verified' THEN now() ELSE null END,now(),%s)
           ON CONFLICT(provider_key,api_name) DO UPDATE SET
             availability=CASE WHEN quant.provider_api_capabilities.availability='verified' AND EXCLUDED.availability IN ('failed','empty')
                               THEN quant.provider_api_capabilities.availability ELSE EXCLUDED.availability END,
             frequency=EXCLUDED.frequency,decision_eligible=EXCLUDED.decision_eligible,
             note=CASE WHEN EXCLUDED.note<>'' THEN EXCLUDED.note ELSE quant.provider_api_capabilities.note END,
             verified_at=CASE WHEN EXCLUDED.availability='verified' THEN now()
                              ELSE quant.provider_api_capabilities.verified_at END,
             last_checked_at=now(),metadata=quant.provider_api_capabilities.metadata || EXCLUDED.metadata""",
        (provider, api_name, availability, contract.frequency, contract.decision_eligible, note[:500], availability, Json(metadata)),
    )


def provider_error_availability(error: str) -> str:
    """Classify explicit denials without mistaking a bad probe for coverage."""
    lowered = error.lower()
    parameter_markers = ("parameter not allowed", "参数不能为空", "missing required parameter", "invalid parameter")
    permission_markers = (
        "api not purchased", "not purchased", "permission denied", "not authorized", "no permission",
        "权限不足", "没有权限", "访问权限", "未购买", "api不存在", "api not found",
    )
    if any(marker in lowered for marker in parameter_markers):
        return "unknown"
    if "404 not found" in lowered or "http 404" in lowered or "接口不存在" in lowered:
        return "unsupported"
    return "unsupported" if any(marker in lowered for marker in permission_markers) else "failed"
