"""Bounded post-close minute-pattern research orchestration."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class StrategyPatternMiningDependencies:
    latest_date: Callable[[], date | None]
    refresh_sources: Callable[[date], Awaitable[dict[str, Any]]]
    sample_candidates: Callable[[date, int, int, list[str] | None], dict[str, Any]]
    open_provider_capabilities: Callable[[str, list[str]], Awaitable[set[str]]]
    minute_capability: str
    fetch_minutes: Callable[[str], Awaitable[list[dict[str, Any]]]]
    intraday_pattern: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]
    review_score: Callable[[dict[str, Any], dict[str, Any], list[str]], dict[str, Any]]
    persist_minute_health: Callable[[int, list[str], int | None], None]
    persist_run: Callable[..., Any]
    run_database: Callable[..., Awaitable[Any]]
    model_version: str
    handled_errors: tuple[type[BaseException], ...]
    max_in_flight: int = 4
    minute_timeout_seconds: float = 10.0


async def run_strategy_pattern_mining(request: Any, dependencies: StrategyPatternMiningDependencies) -> dict[str, Any]:
    """Build bounded replay evidence from already-selected post-close samples.

    This is research-only: it neither imports historical data nor changes live
    thresholds.  The sole network callback is the bounded Tencent minute tape
    supplied by the composition root.
    """
    latest = await dependencies.run_database(dependencies.latest_date)
    as_of_date = request.as_of_date or latest
    if as_of_date is None:
        return {"status": "blocked", "reason": "no daily bars are stored", "samples": []}
    limit_sources = await dependencies.refresh_sources(as_of_date) if request.refresh_limit_sources else {"status": "skipped"}
    selection = await dependencies.run_database(
        dependencies.sample_candidates, as_of_date, request.max_symbols, request.per_cohort, request.focus_symbols,
    )
    candidates = selection.get("candidates", [])
    minute_circuit_open = bool(candidates) and dependencies.minute_capability in await dependencies.open_provider_capabilities(
        "tencent_free", [dependencies.minute_capability],
    )
    semaphore = asyncio.Semaphore(max(1, dependencies.max_in_flight))

    async def replay(item: dict[str, Any]) -> dict[str, Any]:
        try:
            async with semaphore:
                rows = await asyncio.wait_for(
                    dependencies.fetch_minutes(item["symbol"]), timeout=dependencies.minute_timeout_seconds,
                )
            pattern = dependencies.intraday_pattern(rows, item["daily_features"])
            risk_flags = list(item["risk_flags"])
            if item["daily_features"].get("ground_to_sky_daily_shape") and "ground_to_sky_reversal" not in pattern.get("pattern_tags", []):
                risk_flags.append("daily_minute_extreme_path_mismatch")
            review = dependencies.review_score(item, pattern, risk_flags)
            return {
                **item, "limit_context": {**item["limit_context"], **review},
                "intraday_pattern": pattern, "minute_source": "tencent_free_minute", "risk_flags": risk_flags,
            }
        except dependencies.handled_errors as error:
            return {
                **item, "intraday_pattern": {"status": "failed", "error": str(error)[:240], "curve": []},
                "minute_source": "tencent_free_minute", "risk_flags": [*item["risk_flags"], "minute_replay_failed"],
            }

    if minute_circuit_open:
        samples = [{
            **item,
            "intraday_pattern": {
                "status": "blocked", "error": "provider health circuit is open; upstream request skipped", "curve": [],
            },
            "minute_source": "tencent_free_minute", "risk_flags": [*item["risk_flags"], "minute_replay_circuit_open"],
        } for item in candidates]
    else:
        started_at = asyncio.get_running_loop().time()
        samples = await asyncio.gather(*(replay(item) for item in candidates))
        if candidates:
            completed_count = sum(item["intraday_pattern"].get("status") == "completed" for item in samples)
            errors = [
                str(item["intraday_pattern"].get("error") or "minute replay failed")
                for item in samples if item["intraday_pattern"].get("status") != "completed"
            ]
            await dependencies.run_database(
                dependencies.persist_minute_health, completed_count, errors,
                round((asyncio.get_running_loop().time() - started_at) * 1000),
            )

    samples.sort(key=lambda item: (-float(item.get("limit_context", {}).get("review_score") or 0), item["symbol"]))
    failed = [item for item in samples if item["intraday_pattern"].get("status") != "completed"]
    status = "blocked" if minute_circuit_open or not samples else "partial" if failed else "completed"
    pattern_counts: dict[str, int] = {}
    for item in samples:
        for tag in item["intraday_pattern"].get("pattern_tags", []):
            pattern_counts[str(tag)] = pattern_counts.get(str(tag), 0) + 1
    picks = [item for item in samples if item.get("limit_context", {}).get("review_tier") != "research_sample"][:10]
    summary = {
        "selected": len(samples), "picks": len(picks), "minute_completed": len(samples) - len(failed),
        "minute_failed": len(failed), "cohort_counts": selection.get("cohort_counts", {}),
        "pattern_counts": pattern_counts, "limit_pool_rows": selection.get("limit_pool_rows", 0),
        "limit_step_rows": selection.get("limit_step_rows", 0),
        "dragon_leader_market_context": selection.get("dragon_leader_market_context", {}),
    }
    source_status = {
        "daily": "canonical_bars_daily", "limit_sources": limit_sources,
        "minute": {
            "provider": "tencent_free", "status": "circuit_open" if minute_circuit_open else status,
            "completed": len(samples) - len(failed),
            "failed": {item["symbol"]: item["intraday_pattern"].get("error") for item in failed},
        },
        "super_get_minute": "corroborating source when healthy; Tencent is the bounded post-close replay source",
    }
    run_key = hashlib.sha256(f"{dependencies.model_version}:{as_of_date}".encode()).hexdigest()
    run_id = await dependencies.run_database(
        dependencies.persist_run, run_key, as_of_date, status, source_status, summary, samples, timeout_seconds=60,
    )
    return {
        "status": status, "as_of_date": str(as_of_date), "run_id": str(run_id),
        "model_version": dependencies.model_version, "summary": summary, "source_status": source_status,
        "picks": [{**item, "rank": rank} for rank, item in enumerate(picks, start=1)],
        "samples": [{**item, "rank": rank} for rank, item in enumerate(samples, start=1)],
        "notice": "样本用于发现可证伪的盘中形态；地天板只产生研究观察和承接检查，不自动下单。",
    }


__all__ = ["StrategyPatternMiningDependencies", "run_strategy_pattern_mining"]
