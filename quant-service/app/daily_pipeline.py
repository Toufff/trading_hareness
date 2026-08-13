"""Dependency-injected daily market pipeline orchestration."""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable


async def run_pipeline(
    payload: Any,
    *,
    sync_tushare: Callable[[Any], Awaitable[dict[str, Any]]],
    sync_baostock: Callable[[Any], Awaitable[dict[str, Any]]],
    sync_tushare_daily_core: Callable[[date | None], Awaitable[dict[str, Any]]],
    tushare_request: Any,
    snapshot_request: Callable[[date | None], Any],
    build_snapshot: Callable[..., Any],
    recompute_outcomes: Callable[..., Any],
    recompute_scorecards: Callable[..., Any],
    generate_recommendations: Callable[..., Any],
    run_database_blocking: Callable[..., Awaitable[Any]],
    cn_today: Callable[[], date],
) -> dict[str, Any]:
    primary = await sync_tushare(tushare_request(trade_date=payload.as_of_date))
    fallback = None
    if primary["status"] in {"disabled", "partial", "failed"}:
        fallback = await sync_baostock(tushare_request(trade_date=payload.as_of_date))
    core = await sync_tushare_daily_core(payload.as_of_date or cn_today()) if primary["status"] in {"completed", "unchanged", "partial"} else None
    sync = {"primary": primary, "fallback": fallback, "core": core}
    snapshot = await run_database_blocking(build_snapshot, snapshot_request(payload.as_of_date), timeout_seconds=30)
    if snapshot["status"] != "ready":
        return {"status": "blocked", "market_sync": sync, "snapshot": snapshot,
                "reason": "行情数据或质量门禁未满足；没有生成候选池"}
    outcomes = await run_database_blocking(recompute_outcomes, payload.as_of_date, timeout_seconds=60)
    scorecard = await run_database_blocking(recompute_scorecards, payload.as_of_date, timeout_seconds=30)
    result = await run_database_blocking(generate_recommendations, payload, timeout_seconds=30)
    return {"status": "completed", "market_sync": sync, "snapshot": snapshot, "outcomes": outcomes,
            "scorecards": scorecard, "recommendations": result}


__all__ = ["run_pipeline"]
