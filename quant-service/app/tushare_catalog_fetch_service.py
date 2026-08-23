"""Lifecycle orchestration for one bounded Tushare catalog request.

The service has no provider configuration or database singleton.  Its caller
injects the already-governed candidates, rate-limited provider client and
durable fetch-ledger callbacks, which keeps the request contract testable and
prevents a second, less constrained fetch path from appearing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Any, Awaitable, Callable

from fastapi import HTTPException


@dataclass(frozen=True)
class CatalogFetchDependencies:
    realtime_market_hours_apis: set[str] | frozenset[str]
    realtime_market_session: Callable[[str], Awaitable[tuple[bool, str]]]
    provider_candidates: Callable[[str, str], list[Any]]
    circuit_open_provider_keys: Callable[[str, list[Any]], Awaitable[set[str]]]
    run_database: Callable[..., Awaitable[Any]]
    prepare_run: Callable[..., Any]
    persist_success: Callable[..., Any]
    persist_cancel: Callable[..., Any]
    persist_failure: Callable[..., Any]
    persist_blocked: Callable[..., Any]
    call_api: Callable[..., Awaitable[Any]]
    looks_like_response_header: Callable[[list[dict[str, Any]]], bool]
    realtime_rows_are_current: Callable[[str, list[dict[str, Any]]], bool]
    catalog: dict[str, str]
    normalized_apis: set[str] | frozenset[str]
    provider_call_error: type[Exception]
    executor_saturated_error: type[Exception]
    local_capacity_detail: str


async def fetch_catalog(request: Any, deps: CatalogFetchDependencies) -> dict[str, Any]:
    """Fetch, validate and persist one allow-listed catalog response."""
    if request.api_name in deps.realtime_market_hours_apis:
        active, reason = await deps.realtime_market_session(request.api_name)
        if not active:
            raise HTTPException(status_code=409, detail=f"{request.api_name} probe skipped: {reason}")
    canonical_params = json.loads(json.dumps(request.params, ensure_ascii=False, sort_keys=True, default=str))
    candidates = deps.provider_candidates(request.api_name, request.provider)
    if not candidates:
        raise HTTPException(status_code=503, detail=f"no configured provider supports {request.api_name} for {request.provider}")
    blocked_provider_keys = await deps.circuit_open_provider_keys(request.api_name, candidates)
    candidates = [provider for provider in candidates if provider.key not in blocked_provider_keys]
    if not candidates:
        raise HTTPException(status_code=503, detail=f"all configured providers are temporarily circuit-open for {request.api_name}")
    candidate_keys = [provider.key for provider in candidates]
    request_identity: dict[str, Any] = {
        "api_name": request.api_name, "provider": request.provider,
        "provider_candidates": candidate_keys, "params": canonical_params,
        "fields": request.fields, "paginate": request.paginate,
        "page_size": request.page_size, "max_pages": request.max_pages,
        "require_complete": request.require_complete,
    }
    if request.force_refresh:
        request_identity["audit_nonce"] = uuid.uuid4().hex
    request_key = hashlib.sha256(json.dumps(request_identity, sort_keys=True).encode()).hexdigest()
    cached = await deps.run_database(
        deps.prepare_run, request, request_key, candidate_keys, canonical_params, timeout_seconds=60,
    )
    if cached is not None:
        return cached
    provider_started_at = asyncio.get_running_loop().time()
    try:
        result = await deps.call_api(
            request.api_name, canonical_params, request.fields, request.provider,
            paginate=request.paginate, page_size=request.page_size,
            max_rows=request.max_rows, max_pages=request.max_pages,
            require_complete=request.require_complete, blocked_provider_keys=blocked_provider_keys,
        )
        rows = result.rows
        if deps.looks_like_response_header(rows):
            raise deps.provider_call_error("provider returned a header row instead of market data")
        if not deps.realtime_rows_are_current(request.api_name, rows):
            raise deps.provider_call_error(f"provider returned stale realtime rows for {request.api_name}")
        if request.api_name.endswith("_min") or request.api_name.endswith("_min_daily"):
            rows = sorted(rows, key=lambda row: str(
                row.get("time") or row.get("updated_at") or row.get("trade_time")
                or row.get("datetime") or ""
            ), reverse=True)
        truncated = len(rows) > request.max_rows or not result.complete
        if request.require_complete and truncated:
            raise deps.provider_call_error(
                f"{result.provider.key} did not reach a terminal page for {request.api_name} "
                f"within {request.max_pages} pages/{request.max_rows} rows"
            )
        bounded_rows = rows[:request.max_rows]
        provider_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)
        status, normalized_rows = await deps.run_database(
            deps.persist_success, request, request_key, bounded_rows, truncated, result, provider_latency_ms,
            timeout_seconds=60,
        )
        return {
            "status": status, "api_name": request.api_name, "group": deps.catalog[request.api_name],
            "normalized": request.api_name in deps.normalized_apis, "received": len(rows), "stored": len(bounded_rows),
            "provider": result.provider.key,
            "fallback_failures": [{"provider": key, "error": error} for key, error in result.failed_providers],
            "fallback_empty_providers": list(result.empty_providers),
            "normalized_rows": normalized_rows, "truncated": truncated, "complete": not truncated,
            "pages": result.pages, "request_key": request_key,
        }
    except asyncio.CancelledError:
        await deps.run_database(deps.persist_cancel, request_key, request.api_name, candidate_keys)
        raise
    except deps.executor_saturated_error as error:
        await deps.run_database(deps.persist_blocked, request_key, error)
        raise HTTPException(status_code=503, detail=deps.local_capacity_detail) from error
    except Exception as error:  # noqa: BLE001 - durable ledger must close every observed outcome
        provider_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)
        await deps.run_database(
            deps.persist_failure, request_key, request.api_name, candidate_keys, error, provider_latency_ms,
        )
        raise HTTPException(status_code=502, detail=f"Tushare {request.api_name} request failed") from error


__all__ = ["CatalogFetchDependencies", "fetch_catalog"]
