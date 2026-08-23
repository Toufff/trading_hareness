"""Bounded Tushare enrichment adapter for a single-stock research study."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException


@dataclass(frozen=True)
class StockStudyTushareDependencies:
    fetch_catalog: Callable[[Any], Awaitable[dict[str, Any]]]
    run_database: Callable[..., Awaitable[Any]]
    raw_rows_for_request: Callable[[str], list[dict[str, Any]]]
    looks_like_response_header: Callable[[list[dict[str, Any]]], bool]
    is_local_capacity_error: Callable[[HTTPException], bool]
    is_circuit_open_error: Callable[[HTTPException], bool]


async def fetch_stock_study_input(
    label: str, request: Any, deps: StockStudyTushareDependencies,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch one non-fatal study input while preserving its raw-ledger source."""
    try:
        outcome = await asyncio.wait_for(deps.fetch_catalog(request), timeout=12)
        rows = await deps.run_database(deps.raw_rows_for_request, str(outcome["request_key"]))
        if deps.looks_like_response_header(rows):
            return ({"source": label, "api_name": request.api_name, "provider": outcome.get("provider"),
                     "status": "invalid_response", "received": 0, "stored": outcome.get("stored", 0),
                     "error": "provider returned a header row instead of market data"}, [])
        return ({"source": label, "api_name": request.api_name, "provider": outcome.get("provider"),
                 "status": outcome["status"], "received": outcome.get("received", outcome.get("stored", 0)),
                 "stored": outcome.get("stored", 0), "fallback_failures": outcome.get("fallback_failures", [])}, rows)
    except asyncio.TimeoutError:
        return ({"source": label, "api_name": request.api_name, "provider": request.provider,
                 "status": "blocked", "received": 0, "stored": 0,
                 "error": "study source exceeded 12 second local budget; provider outcome was not observed"}, [])
    except HTTPException as error:
        status = "blocked" if deps.is_local_capacity_error(error) else "circuit_open" if deps.is_circuit_open_error(error) else "failed"
        return ({"source": label, "api_name": request.api_name, "provider": request.provider,
                 "status": status, "received": 0, "stored": 0, "error": str(error.detail)}, [])


__all__ = ["StockStudyTushareDependencies", "fetch_stock_study_input"]
