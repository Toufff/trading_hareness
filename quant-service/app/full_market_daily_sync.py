"""Dependency-injected post-close full-market daily synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json


async def sync(
    request: Any,
    *,
    provider_candidates: Callable[..., list[Any]],
    cn_date: Callable[[], Any],
    call_tushare_api: Callable[..., Awaitable[Any]],
    looks_like_response_header: Callable[[list[dict[str, Any]]], bool],
    tushare_date: Callable[[Any], Any],
    persist_tushare_rows: Callable[..., int],
    run_database_blocking: Callable[..., Awaitable[Any]],
    persist_tushare_fetch_blocked: Callable[..., Any],
    db: Any,
    safe_error_detail: Callable[[str, int], str],
    provider_call_error: type[Exception],
    executor_saturated_error: type[Exception],
    record_provider_success: Callable[..., Any],
    record_provider_failure: Callable[..., Any],
    record_provider_api_capability: Callable[..., Any],
    record_provider_empty: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    candidates = provider_candidates("daily", request.provider)
    if not candidates:
        return {"status": "blocked", "reason": "no configured provider supports daily"}
    trade_date = request.trade_date or cn_date()
    # The quality gate is part of the request identity.  A bounded/manual
    # import that accepted a partial cross-section must never satisfy the
    # post-close all-A refresh that requires the full population.
    request_key = hashlib.sha256(json.dumps({
        "capability": "daily_all_a", "trade_date": str(trade_date),
        "provider": request.provider, "minimum_rows": request.minimum_rows,
    }, sort_keys=True).encode()).hexdigest()

    def prepare_run() -> dict[str, Any] | None:
        with db.transaction() as connection:
            prior = connection.execute("SELECT status,row_count FROM quant.fetch_runs WHERE request_key=%s", (request_key,)).fetchone()
            if prior and prior["status"] == "completed":
                return {"status": "unchanged", "trade_date": str(trade_date), "imported": prior["row_count"], "request_key": request_key}
            connection.execute(
                """INSERT INTO quant.fetch_runs(provider_key,capability,trade_date,request_key,status,attempt_count,started_at,metadata)
                   VALUES(%s,'daily_all_a',%s,%s,'running',1,now(),%s)
                   ON CONFLICT(request_key) DO UPDATE SET status='running',attempt_count=quant.fetch_runs.attempt_count+1,
                     started_at=now(),finished_at=null,error_class=null,error_message=null""",
                (candidates[0].key, trade_date, request_key, Json({"minimum_rows": request.minimum_rows})),
            )
        return None

    unchanged = await run_database_blocking(prepare_run)
    if unchanged:
        return unchanged
    provider_started_at = asyncio.get_running_loop().time()
    result = None
    rejected_provider_errors: list[tuple[str, str]] = []
    empty_provider_keys: list[str] = []
    try:
        # ``call_with_fallback`` only falls through on an empty response.  A
        # nonempty truncated cross-section is equally unusable for this
        # endpoint, so evaluate each configured auto candidate independently
        # against the full-market gate.  Rows are never merged between sources.
        valid_rows: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                candidate_result = await call_tushare_api(
                    "daily", {"trade_date": trade_date.strftime("%Y%m%d")},
                    "ts_code,trade_date,open,high,low,close,pre_close,vol,amount", candidate.name,
                )
            except executor_saturated_error:
                raise
            except Exception as error:  # noqa: BLE001 - reject one source, then try the next audited contract
                rejected_provider_errors.append((candidate.key, safe_error_detail(str(error), 300)))
                continue
            rows = candidate_result.rows
            if not rows:
                empty_provider_keys.append(candidate_result.provider.key)
                rejected_provider_errors.append((candidate_result.provider.key, "daily returned no rows"))
                continue
            if looks_like_response_header(rows):
                rejected_provider_errors.append((candidate_result.provider.key, "provider returned a header row instead of market daily data"))
                continue
            valid_by_symbol = {
                str(row["ts_code"]).upper(): row for row in rows
                if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row.get("ts_code") or "").upper())
                and tushare_date(row.get("trade_date")) == trade_date
            }
            candidate_valid_rows = list(valid_by_symbol.values())
            if len(candidate_valid_rows) < request.minimum_rows:
                rejected_provider_errors.append((
                    candidate_result.provider.key,
                    f"daily returned {len(candidate_valid_rows)} valid A-share rows; expected at least {request.minimum_rows}",
                ))
                continue
            result = candidate_result
            valid_rows = candidate_valid_rows
            break
        if result is None:
            detail = " | ".join(f"{provider_key}: {message}" for provider_key, message in rejected_provider_errors)
            raise provider_call_error(detail or "no configured provider returned a complete all-A daily cross-section")
        observed_at = datetime.now(timezone.utc)
        provider_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)

        def persist_result() -> int:
            # A full A-share cross-section is exactly the "post-close stage"
            # case the connection-level statement_timeout default is too
            # tight for; opt into the long-task budget explicitly.
            with db.long_transaction() as connection:
                normalized = persist_tushare_rows(connection, "daily", request_key, valid_rows, result.provider.key, observed_at)
                connection.execute("UPDATE quant.fetch_runs SET status='completed',row_count=%s,finished_at=now() WHERE request_key=%s", (len(valid_rows), request_key))
                record_provider_success(connection, result.provider.key, "daily_all_a", len(valid_rows), provider_latency_ms)
                record_provider_api_capability(connection, result.provider.key, "daily", "verified", len(valid_rows), "Full-market post-close daily bars refreshed.")
                for provider_key, error in [*result.failed_providers, *rejected_provider_errors]:
                    if provider_key == result.provider.key:
                        continue
                    record_provider_failure(connection, provider_key, "daily", error, provider_latency_ms)
                    record_provider_api_capability(connection, provider_key, "daily", "failed", note=error)
            return normalized

        # A full A-share cross-section promotes several thousand raw and
        # canonical rows in one transaction. The generic 10-second repository
        # timeout can expire while PostgreSQL is still committing, producing a
        # false blocked response after a successful write. Keep this bounded
        # but large enough for the resource-capped edge host.
        normalized = await run_database_blocking(persist_result, timeout_seconds=180)
        return {"status": "completed", "trade_date": str(trade_date), "imported": len(valid_rows), "normalized_rows": normalized,
                "provider": result.provider.key, "request_key": request_key}
    except executor_saturated_error as error:
        await run_database_blocking(persist_tushare_fetch_blocked, request_key, error)
        return {"status": "blocked", "trade_date": str(trade_date), "reason": safe_error_detail(str(error), 500), "request_key": request_key}
    except Exception as error:  # noqa: BLE001 - provider failures are persisted and returned safely
        # An empty source is only a valid "not published yet" conclusion when
        # every audited candidate was empty.  A mixed empty/error result is a
        # provider failure and must remain retryable.
        empty_providers = [*empty_provider_keys] if len(empty_provider_keys) == len(candidates) else []
        failure_latency_ms = round((asyncio.get_running_loop().time() - provider_started_at) * 1000)
        # ``except ... as error`` is implicitly deleted once the block exits;
        # capture the text before defining a nested closure over it so a
        # deferred call cannot see a NameError instead of the real failure.
        error_text = str(error)

        def persist_failure() -> None:
            with db.transaction() as connection:
                detail = safe_error_detail(error_text, 1000)
                connection.execute(
                    "UPDATE quant.fetch_runs SET status='failed',finished_at=now(),error_class=%s,error_message=%s WHERE request_key=%s",
                    ("source_empty" if empty_providers else "provider_error", detail, request_key),
                )
                if empty_providers:
                    for provider_key in empty_providers:
                        # A valid empty full-market response ("not published
                        # yet") is not the same evidence as a real success:
                        # it must not reset a genuinely failing provider's
                        # consecutive-failure streak back to zero.
                        if record_provider_empty is not None:
                            record_provider_empty(connection, provider_key, "daily_all_a")
                        else:
                            record_provider_success(connection, provider_key, "daily_all_a", 0)
                        record_provider_api_capability(connection, provider_key, "daily", "empty", 0,
                                                       "Valid empty full-market response; post-close data is not published yet.")
                else:
                    failures = rejected_provider_errors or [(candidates[0].key, detail)]
                    for provider_key, provider_error in failures:
                        record_provider_failure(connection, provider_key, "daily_all_a", provider_error, failure_latency_ms)
                        record_provider_api_capability(connection, provider_key, "daily", "failed", note=provider_error)

        await run_database_blocking(persist_failure)
        return {"status": "blocked", "trade_date": str(trade_date), "reason": safe_error_detail(error_text, 500),
                "fallback_empty_providers": empty_providers, "request_key": request_key}


__all__ = ["sync"]
