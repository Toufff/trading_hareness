"""Bounded realtime and Tushare capability-probe orchestration.

This module owns probe matrix expansion, market-hours gates, bounded
concurrency and status aggregation.  It deliberately receives provider calls
and persistence operations as dependencies, so API composition remains the
only place that knows about the database, HTTP exceptions and executor setup.
Catalog declarations remain just declarations until a probe returns rows.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from .request_models import TushareFetchRequest


async def probe_realtime(
    payload: Any,
    *,
    realtime_probe_matrix: Callable[..., list[tuple[str, dict[str, Any]]]],
    default_probe_params: Callable[..., dict[str, Any] | None],
    realtime_market_session: Callable[[str], Awaitable[tuple[bool, str]]],
    provider_candidates: Callable[[str, str], Any],
    fetch: Callable[[str, TushareFetchRequest], Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]]],
    max_concurrency: int = 4,
) -> dict[str, Any]:
    """Probe real-time families without hiding unsupported physical routes."""
    matrix = realtime_probe_matrix(
        symbol=payload.symbols[0], frequency=payload.frequency, etf_symbol=payload.etf_symbol,
        index_symbol=payload.index_symbol, sw_symbol=payload.sw_symbol,
        futures_symbol=payload.futures_symbol,
    )
    for symbol in payload.symbols[1:]:
        for api_name in ("rt_k", "rt_min", "rt_min_daily"):
            params = default_probe_params(api_name, symbol=symbol, frequency=payload.frequency)
            if params is not None:
                matrix.append((api_name, params))

    runnable: list[tuple[str, str, dict[str, Any], TushareFetchRequest]] = []
    results: list[dict[str, Any]] = []
    for api_name, params in matrix:
        active, reason = await realtime_market_session(api_name)
        for provider in ("primary", "super_sdk", "super_get"):
            if not provider_candidates(api_name, provider):
                results.append({"provider": provider, "api_name": api_name, "params": params,
                                "status": "skipped", "availability": "unsupported",
                                "reason": "physical provider has no verified route for this realtime API"})
                continue
            if not active:
                results.append({"provider": provider, "api_name": api_name, "params": params,
                                "status": "skipped", "availability": "declared", "reason": reason})
                continue
            runnable.append((provider, api_name, params, TushareFetchRequest(
                api_name=api_name, provider=provider, params=params,
                max_rows=260 if api_name.endswith("_daily") else 10, force_refresh=True,
            )))

    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def run_probe(provider: str, api_name: str, params: dict[str, Any], request: TushareFetchRequest) -> dict[str, Any]:
        async with semaphore:
            outcome, _ = await fetch(f"{provider} {api_name}", request)
        return {"provider": provider, "api_name": api_name, "params": params, **outcome}

    if runnable:
        results.extend(await asyncio.gather(*(run_probe(*item) for item in runnable)))
    completed = [item for item in results if item["status"] in {"completed", "partial", "unchanged", "empty"}]
    failed = [item for item in results if item["status"] == "failed"]
    status = "skipped" if not runnable else "completed" if not failed else "partial" if completed else "failed"
    return {"status": status, "symbols": payload.symbols, "frequency": payload.frequency,
            "api_count": len({item["api_name"] for item in results}), "results": results}


async def audit_tushare_capabilities(
    payload: Any,
    *,
    today: Callable[[], date],
    api_capability: Callable[[str], Any],
    default_probe_params: Callable[..., dict[str, Any] | None],
    historical_minute_apis: frozenset[str],
    realtime_market_hours_apis: frozenset[str],
    realtime_market_session: Callable[[str], Awaitable[tuple[bool, str]]],
    fetch_catalog: Callable[[TushareFetchRequest], Awaitable[dict[str, Any]]],
    record_timeout: Callable[[str, str], Awaitable[None]],
    load_observation: Callable[[str, str], Awaitable[dict[str, Any] | None]],
    is_local_capacity_error: Callable[[HTTPException], bool],
    is_circuit_open_error: Callable[[HTTPException], bool],
    timeout_seconds: float = 25,
) -> dict[str, Any]:
    """Run bounded explicit probes without elevating catalog entries to proof."""
    results: list[dict[str, Any]] = []
    as_of = payload.as_of_date or today()
    for api_name in payload.api_names:
        contract = api_capability(api_name)
        params = default_probe_params(api_name, symbol=payload.symbol, as_of=as_of)
        for provider in payload.providers:
            if api_name in historical_minute_apis:
                results.append({"api_name": api_name, "provider": provider, "status": "skipped",
                                "availability": "declared", "reason": "offline_files_only"})
                continue
            if api_name in realtime_market_hours_apis:
                active, reason = await realtime_market_session(api_name)
                if not active:
                    results.append({"api_name": api_name, "provider": provider, "status": "skipped",
                                    "availability": "declared", "reason": reason})
                    continue
            if params is None:
                results.append({"api_name": api_name, "provider": provider, "status": "skipped",
                                "availability": "declared", "reason": "manual_parameters_required"})
                continue
            try:
                outcome = await asyncio.wait_for(
                    fetch_catalog(TushareFetchRequest(
                        api_name=api_name, provider=provider, params=params,
                        max_rows=payload.max_rows, force_refresh=True,
                    )),
                    timeout=timeout_seconds,
                )
                availability = "verified" if int(outcome.get("stored", 0)) > 0 else "empty"
                results.append({"api_name": api_name, "provider": provider, "params": params,
                                "status": outcome["status"], "availability": availability,
                                "received": outcome.get("received", 0), "stored": outcome.get("stored", 0),
                                "request_key": outcome.get("request_key")})
            except TimeoutError:
                await record_timeout(provider, api_name)
                results.append({"api_name": api_name, "provider": provider, "params": params,
                                "status": "failed", "availability": "failed", "reason": "audit_timeout_25s",
                                "contract_status": contract.status})
            except HTTPException as error:
                if is_local_capacity_error(error):
                    results.append({"api_name": api_name, "provider": provider, "params": params,
                                    "status": "blocked", "availability": "local_capacity",
                                    "reason": str(error.detail), "contract_status": contract.status})
                    continue
                if is_circuit_open_error(error):
                    results.append({"api_name": api_name, "provider": provider, "params": params,
                                    "status": "circuit_open", "availability": "circuit_open",
                                    "reason": str(error.detail), "contract_status": contract.status})
                    continue
                observation = await load_observation(provider, api_name)
                results.append({"api_name": api_name, "provider": provider, "params": params,
                                "status": "failed", "availability": observation["availability"] if observation else "failed",
                                "reason": str(error.detail), "contract_status": contract.status})
    attempted = [item for item in results if item["status"] != "skipped"]
    completed = [item for item in attempted if item["status"] in {"completed", "partial", "unchanged", "empty"}]
    failures = [item for item in attempted if item["status"] == "failed"]
    blocked = [item for item in attempted if item["status"] in {"blocked", "circuit_open"}]
    status = (
        "skipped" if not attempted else
        "blocked" if blocked and not completed and not failures else
        "completed" if completed and not failures and not blocked else
        "partial" if completed or blocked else "failed"
    )
    return {"status": status, "as_of_date": str(as_of), "symbol": payload.symbol,
            "requested_apis": payload.api_names, "requested_providers": payload.providers, "results": results}


__all__ = ["audit_tushare_capabilities", "probe_realtime"]
