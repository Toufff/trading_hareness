"""Durable local ledger transitions for bounded Tushare catalog fetches.

The transport, provider selection, retry and rate-limit policy deliberately
remain in the catalog fetch orchestrator.  This module owns only the atomic
database transitions around already-bounded rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass(frozen=True)
class TushareFetchLedgerDependencies:
    database: Any
    json_value: Callable[[Any], Any]
    looks_like_response_header: Callable[[list[dict[str, Any]]], bool]
    normalize_cached_rows: Callable[..., int]
    persist_rows: Callable[..., int]
    record_provider_failure: Callable[..., None]
    record_provider_success: Callable[..., None]
    record_provider_capability: Callable[..., None]
    provider_error_availability: Callable[[str], str]
    provider_call_error: type[Exception]
    safe_error_detail: Callable[..., str]


def prepare_run(
    request: Any,
    request_key: str,
    candidate_keys: list[str],
    canonical_params: dict[str, Any],
    deps: TushareFetchLedgerDependencies,
) -> dict[str, Any] | None:
    """Atomically reuse complete evidence or record a bounded run as active."""
    with deps.database.transaction() as connection:
        existing = connection.execute(
            "SELECT status,row_count FROM quant.fetch_runs WHERE request_key=%s", (request_key,),
        ).fetchone()
        if existing and existing["status"] == "completed":
            saved_rows = connection.execute(
                "SELECT provider_key,row_data FROM quant.tushare_raw_records WHERE request_key=%s ORDER BY record_index",
                (request_key,),
            ).fetchall()
            if len(saved_rows) == int(existing["row_count"] or 0):
                saved_provider = str(saved_rows[0]["provider_key"]) if saved_rows else candidate_keys[0]
                cached_rows = [dict(row["row_data"]) for row in saved_rows]
                if deps.looks_like_response_header(cached_rows):
                    return {
                        "status": "invalid_response", "api_name": request.api_name, "request_key": request_key,
                        "provider": saved_provider, "stored": existing["row_count"], "normalized_rows": 0,
                        "error": "cached provider response is a header row, not market data",
                    }
                normalized_rows = deps.normalize_cached_rows(
                    connection, request.api_name, cached_rows, datetime.now(timezone.utc), saved_provider,
                )
                return {
                    "status": "unchanged", "api_name": request.api_name, "request_key": request_key,
                    "provider": saved_provider, "stored": existing["row_count"], "normalized_rows": normalized_rows,
                    "complete": True,
                }
        connection.execute(
            """INSERT INTO quant.fetch_runs(provider_key,capability,request_key,status,attempt_count,started_at,metadata)
               VALUES(%s,%s,%s,'running',1,now(),%s)
               ON CONFLICT(request_key) DO UPDATE SET status='running',attempt_count=quant.fetch_runs.attempt_count+1,
                 started_at=now(),finished_at=null,error_class=null,error_message=null""",
            (candidate_keys[0], request.api_name, request_key,
             deps.json_value({
                 "provider": request.provider, "provider_candidates": candidate_keys, "params": canonical_params,
                 "fields": request.fields, "max_rows": request.max_rows, "paginate": request.paginate,
                 "page_size": request.page_size, "max_pages": request.max_pages,
                 "require_complete": request.require_complete,
             })),
        )
    return None


def persist_success(
    request: Any,
    request_key: str,
    bounded_rows: list[dict[str, Any]],
    truncated: bool,
    result: Any,
    deps: TushareFetchLedgerDependencies,
    provider_latency_ms: int | None = None,
) -> tuple[str, int]:
    """Persist bounded raw evidence, canonical rows and provider health together."""
    with deps.database.transaction() as connection:
        normalized_rows = deps.persist_rows(
            connection, request.api_name, request_key, bounded_rows, result.provider.key, datetime.now(timezone.utc),
        )
        status = "partial" if truncated else "completed"
        connection.execute(
            """UPDATE quant.fetch_runs SET status=%s,row_count=%s,finished_at=now(),error_class=%s,error_message=%s
               WHERE request_key=%s""",
            (status, len(bounded_rows), "row_cap" if truncated else None,
             f"response exceeded local cap of {request.max_rows} rows" if truncated else None, request_key),
        )
        for provider_key, error in result.failed_providers:
            deps.record_provider_failure(connection, provider_key, request.api_name, error, provider_latency_ms)
            deps.record_provider_capability(
                connection, provider_key, request.api_name, deps.provider_error_availability(error), note=error,
            )
        for provider_key in result.empty_providers:
            if provider_key != result.provider.key:
                deps.record_provider_capability(
                    connection, provider_key, request.api_name, "empty", 0,
                    "Valid empty response; the next audited provider was tried without merging sources.",
                )
        deps.record_provider_success(connection, result.provider.key, request.api_name, len(bounded_rows), provider_latency_ms)
        capability_note = "Provider returned real rows; local storage kept a bounded prefix." if truncated else ""
        deps.record_provider_capability(
            connection, result.provider.key, request.api_name, "verified" if bounded_rows else "empty",
            len(bounded_rows), capability_note,
        )
    return status, normalized_rows


def persist_cancel(
    request_key: str,
    _api_name: str,
    _candidate_keys: list[str],
    deps: TushareFetchLedgerDependencies,
) -> None:
    """Close local caller cancellation without producing a provider penalty."""
    with deps.database.transaction() as connection:
        connection.execute(
            "UPDATE quant.fetch_runs SET status='blocked',finished_at=now(),error_class='caller_cancelled',error_message='Request cancelled by the caller timeout before provider outcome' WHERE request_key=%s",
            (request_key,),
        )


def persist_failure(
    request_key: str,
    api_name: str,
    candidate_keys: list[str],
    error: Exception,
    deps: TushareFetchLedgerDependencies,
    provider_latency_ms: int | None = None,
) -> None:
    safe_error = deps.safe_error_detail(str(error), 1000)
    with deps.database.transaction() as connection:
        connection.execute(
            "UPDATE quant.fetch_runs SET status='failed',finished_at=now(),error_class='provider_error',error_message=%s WHERE request_key=%s",
            (safe_error, request_key),
        )
        provider_failures = error.failures if isinstance(error, deps.provider_call_error) and error.failures else tuple(
            (provider_key, deps.safe_error_detail(str(error))) for provider_key in candidate_keys
        )
        for provider_key, provider_error in provider_failures:
            safe_provider_error = deps.safe_error_detail(str(provider_error))
            deps.record_provider_failure(connection, provider_key, api_name, safe_provider_error, provider_latency_ms)
            deps.record_provider_capability(
                connection, provider_key, api_name, deps.provider_error_availability(safe_provider_error), note=safe_provider_error,
            )


def persist_blocked(request_key: str, error: Exception, deps: TushareFetchLedgerDependencies) -> None:
    """Close a row for local backpressure without assigning provider blame."""
    detail = deps.safe_error_detail(str(error), 300)
    with deps.database.transaction() as connection:
        connection.execute(
            """UPDATE quant.fetch_runs SET status='blocked',finished_at=now(),
               error_class='local_capacity',error_message=%s WHERE request_key=%s""",
            (detail, request_key),
        )


__all__ = [
    "TushareFetchLedgerDependencies", "persist_blocked", "persist_cancel", "persist_failure", "persist_success", "prepare_run",
]
