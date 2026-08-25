"""Pure assembly of declared leased background-task specifications."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .runtime_tasks import BackgroundTaskSpec


def build_specs(*, interval_seconds: int, enabled: dict[str, bool], loops: dict[str, Callable[[], Any]]) -> tuple[BackgroundTaskSpec, ...]:
    """Keep task labels, enablement and factories outside the ASGI root."""
    return tuple(
        BackgroundTaskSpec(label, bool(enabled.get(label)), loops[label])
        for label in (
            "intraday_monitor", "super_get_fast_quote", "strategy_review", "post_close_strategy",
            "ten_day_leader_rotation", "daily_strategy_summary", "ths_member_backfill",
            "all_board_member_backfill", "minute_profile_capture", "tencent_order_book", "board_flow_curve",
        )
    )


__all__ = ["build_specs"]
