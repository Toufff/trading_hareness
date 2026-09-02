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
             last_success_at=now(),last_error=null,
             last_latency_ms=COALESCE(EXCLUDED.last_latency_ms,quant.provider_health.last_latency_ms),
             last_row_count=EXCLUDED.last_row_count,updated_at=now()""",
        (provider, capability, latency_ms, rows),
    )


def record_provider_empty_result(connection: Any, provider: str, capability: str,
                                 latency_ms: int | None = None) -> None:
    """Record a valid-but-empty response without resetting the failure streak.

    A full-market cross-section provider legitimately returns zero rows
    before the post-close publish window; that must remain distinguishable
    from success.  ``record_provider_success`` used to be called with
    ``rows=0`` for this case, which reset ``consecutive_failures`` to zero and
    let a genuinely failing/misconfigured key look healthy indefinitely as
    long as it kept answering empty instead of erroring.
    """
    provider_requests_total.labels(provider, capability, "empty").inc()
    if latency_ms is not None and latency_ms >= 0:
        provider_latency_seconds.labels(provider, capability).observe(latency_ms / 1000)
    connection.execute(
        """INSERT INTO quant.provider_health(provider_key,capability,market,consecutive_failures,last_success_at,last_latency_ms,last_row_count)
           VALUES(%s,%s,'cn',0,now(),%s,0)
           ON CONFLICT(provider_key,capability,market) DO UPDATE SET
             last_success_at=now(),
             last_latency_ms=COALESCE(EXCLUDED.last_latency_ms,quant.provider_health.last_latency_ms),
             last_row_count=0,updated_at=now()""",
        (provider, capability, latency_ms),
    )


def record_provider_failure(connection: Any, provider: str, capability: str, error: str,
                           latency_ms: int | None = None, *, error_class: str | None = None,
                           retry_after_seconds: float | None = None) -> None:
    """Record a provider failure, classifying rate limits and bad credentials.

    ``error_class="rate_limited"``/``"unauthorized"`` deliberately do not
    increment ``consecutive_failures``: a supplier explicitly asking the
    caller to slow down, or a credential that was rejected outright, is not
    the same signal as intermittent upstream flakiness, and folding it into
    the same circuit breaker tripped on correct backpressure behaviour or
    permanently parked a fixable bad key behind the generic 5-minute circuit.
    """
    provider_requests_total.labels(provider, capability, "failure").inc()
    if latency_ms is not None and latency_ms >= 0:
        provider_latency_seconds.labels(provider, capability).observe(latency_ms / 1000)
    detail = safe_error_detail(error, 500)
    if error_class == "rate_limited":
        pause_seconds = min(300.0, max(1.0, float(retry_after_seconds or 30.0)))
        connection.execute(
            """INSERT INTO quant.provider_health(provider_key,capability,market,consecutive_failures,last_failure_at,last_error,last_latency_ms,circuit_open_until)
               VALUES(%s,%s,'cn',0,now(),%s,%s,now() + (%s * interval '1 second'))
               ON CONFLICT(provider_key,capability,market) DO UPDATE SET
                 last_failure_at=now(),last_error=EXCLUDED.last_error,
                 last_latency_ms=COALESCE(EXCLUDED.last_latency_ms,quant.provider_health.last_latency_ms),
                 circuit_open_until=now() + (%s * interval '1 second'),updated_at=now()""",
            (provider, capability, f"rate_limited: {detail}", latency_ms, pause_seconds, pause_seconds),
        )
        return
    if error_class == "unauthorized":
        connection.execute(
            """INSERT INTO quant.provider_health(provider_key,capability,market,consecutive_failures,last_failure_at,last_error,last_latency_ms,circuit_open_until)
               VALUES(%s,%s,'cn',0,now(),%s,%s,now() + interval '30 minutes')
               ON CONFLICT(provider_key,capability,market) DO UPDATE SET
                 last_failure_at=now(),last_error=EXCLUDED.last_error,
                 last_latency_ms=COALESCE(EXCLUDED.last_latency_ms,quant.provider_health.last_latency_ms),
                 circuit_open_until=now() + interval '30 minutes',updated_at=now()""",
            (provider, capability, f"unauthorized: {detail}", latency_ms),
        )
        return
    connection.execute(
        """INSERT INTO quant.provider_health(provider_key,capability,market,consecutive_failures,last_failure_at,last_error,last_latency_ms,circuit_open_until)
           VALUES(%s,%s,'cn',1,now(),%s,%s,null)
           ON CONFLICT(provider_key,capability,market) DO UPDATE SET consecutive_failures=quant.provider_health.consecutive_failures+1,
             last_failure_at=now(),last_error=EXCLUDED.last_error,
             last_latency_ms=COALESCE(EXCLUDED.last_latency_ms,quant.provider_health.last_latency_ms),
             circuit_open_until=CASE WHEN quant.provider_health.consecutive_failures+1 >= 3 THEN now()+interval '5 minutes' ELSE null END,
             updated_at=now()""",
        (provider, capability, detail, latency_ms),
    )


def record_provider_api_capability(connection: Any, provider: str, api_name: str, availability: str,
                                   rows: int | None = None, note: str = "") -> None:
    """Keep permission/probe evidence distinct from generic request health."""
    contract = api_capability(api_name)
    metadata = {**catalog_metadata(api_name), "last_observation": availability,
                **({"last_row_count": rows} if rows is not None else {})}
    if note and availability in {"failed", "empty"}:
        # Availability answers "has this physical route ever been verified?".
        # The latest observation is separate evidence; never overwrite the
        # durable verification note with a transient timeout/valid empty.
        metadata["last_observation_note"] = safe_error_detail(note, 500)
    elif note and availability == "verified":
        metadata["verified_note"] = note[:500]
    connection.execute(
        """INSERT INTO quant.provider_api_capabilities(provider_key,api_name,availability,frequency,decision_eligible,note,verified_at,last_checked_at,metadata)
           VALUES(%s,%s,%s,%s,%s,%s,CASE WHEN %s='verified' THEN now() ELSE null END,now(),%s)
           ON CONFLICT(provider_key,api_name) DO UPDATE SET
             availability=CASE WHEN quant.provider_api_capabilities.availability='verified' AND EXCLUDED.availability IN ('failed','empty')
                               THEN quant.provider_api_capabilities.availability ELSE EXCLUDED.availability END,
             frequency=EXCLUDED.frequency,decision_eligible=EXCLUDED.decision_eligible,
             note=CASE WHEN quant.provider_api_capabilities.availability='verified'
                              AND EXCLUDED.availability IN ('failed','empty')
                           THEN quant.provider_api_capabilities.note
                       WHEN EXCLUDED.note<>'' THEN EXCLUDED.note
                       ELSE quant.provider_api_capabilities.note END,
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
