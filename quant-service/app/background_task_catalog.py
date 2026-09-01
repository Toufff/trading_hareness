"""Pure assembly of declared leased background-task specifications."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .runtime_tasks import BackgroundTaskSpec


def build_specs(*, interval_seconds: int, enabled: dict[str, bool], loops: dict[str, Callable[[], Any]]) -> tuple[BackgroundTaskSpec, ...]:
    """Keep task labels, enablement and factories outside the ASGI root."""
    labels = (
            "intraday_monitor", "super_get_fast_quote", "strategy_review", "post_close_strategy",
            "ten_day_leader_rotation", "daily_strategy_summary", "ths_member_backfill",
            "all_board_member_backfill", "minute_profile_capture", "tencent_order_book", "board_flow_curve",
            "market_event_capture",
            "all_a_level1_snapshot",
    )
    # Keep the pure catalog helper backwards-compatible for external callers
    # that inject the pre-0076 loop set; the production composition supplies
    # every declared factory and is still checked by validate_runtime_task_specs.
    return tuple(
        BackgroundTaskSpec(label, bool(enabled.get(label)), loops[label])
        for label in labels if label in loops
    )


__all__ = ["build_specs"]
