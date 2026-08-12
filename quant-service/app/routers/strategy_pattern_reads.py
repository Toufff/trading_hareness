"""HTTP assembly for persisted pattern-mining research reads."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from ..strategy_pattern_read_model import latest_strategy_pattern_mining


def build_strategy_pattern_reads_router(
    database: Any,
    merge_limit_pool_sources_fn: Callable[..., dict[str, Any]],
    limit_board_count_fn: Callable[[Any], int],
    strategy_json_safe_fn: Callable[[Any], Any],
    post_close_limit_daily_features_fn: Callable[[list[dict[str, Any]]], dict[str, Any]],
    post_close_exact_board_context_fn: Callable[[Any], dict[str, Any]],
    post_close_tushare_lhb_context_fn: Callable[[Any], dict[str, Any]],
) -> APIRouter:
    router = APIRouter(tags=["strategy-pattern-reads"])

    @router.get("/api/v1/strategy/pattern-mining/latest")
    def latest() -> dict[str, Any]:
        return latest_strategy_pattern_mining(
            database, merge_limit_pool_sources_fn, limit_board_count_fn, strategy_json_safe_fn,
            post_close_limit_daily_features_fn, post_close_exact_board_context_fn, post_close_tushare_lhb_context_fn,
        )

    return router


__all__ = ["build_strategy_pattern_reads_router"]
