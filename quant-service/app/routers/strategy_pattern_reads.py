"""HTTP assembly for persisted pattern-mining research reads."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from ..strategy_pattern_read_model import latest_strategy_pattern_mining
from ..async_strategy_pattern_read_repository import latest_strategy_pattern_mining as async_latest_strategy_pattern_mining


def build_strategy_pattern_reads_router(
    database: Any,
    merge_limit_pool_sources_fn: Callable[..., dict[str, Any]],
    limit_board_count_fn: Callable[[Any], int],
    strategy_json_safe_fn: Callable[[Any], Any],
    post_close_limit_daily_features_fn: Callable[[list[dict[str, Any]]], dict[str, Any]],
    post_close_exact_board_context_fn: Callable[[Any], dict[str, Any]],
    post_close_tushare_lhb_context_fn: Callable[[Any], dict[str, Any]],
    async_database: Any | None = None,
    database_runner: Callable[..., Any] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["strategy-pattern-reads"])

    @router.get("/api/v1/strategy/pattern-mining/latest")
    async def latest() -> dict[str, Any]:
        fn = async_latest_strategy_pattern_mining if async_database else latest_strategy_pattern_mining
        return await fn(async_database, merge_limit_pool_sources_fn, limit_board_count_fn, strategy_json_safe_fn,
            post_close_limit_daily_features_fn, post_close_exact_board_context_fn, post_close_tushare_lhb_context_fn,
            database_runner=database_runner) if async_database else fn(
            database, merge_limit_pool_sources_fn, limit_board_count_fn, strategy_json_safe_fn,
            post_close_limit_daily_features_fn, post_close_exact_board_context_fn, post_close_tushare_lhb_context_fn,
        )

    return router


__all__ = ["build_strategy_pattern_reads_router"]
