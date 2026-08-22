"""Post-close, research-only screen for possible next-session limit continuation.

The screen deliberately uses only facts published after a limit-up session:
the completed ladder label and the final seal relative to free float.  It is a
next-session review queue, never an intraday chase or an executable order.
"""

from __future__ import annotations

from typing import Any, Callable


MODEL_VERSION = "limit-continuation-v1"
MIN_BOARD_COUNT = 2
MIN_SEAL_TO_FLOAT = 0.015


def continuation_watch(
    item: dict[str, Any], *, number: Callable[[Any], float | None], board_count: Callable[[Any], int],
) -> dict[str, Any]:
    """Describe a transparent post-close continuation-review candidate.

    ``open_num`` is evidence when supplied, but is intentionally not a gate:
    its historical availability is incomplete.  The caller must ensure this
    runs only after the final limit-pool record is available.
    """
    symbol = str(item.get("ts_code") or item.get("symbol") or "").upper()
    sources = {str(source) for source in item.get("sources") or ()}
    limit_amount = number(item.get("limit_amount"))
    free_float = number(item.get("free_float"))
    streak = max(int(board_count(item.get("tag")) or 0), int(number(item.get("streak_count")) or 0))
    seal_to_float = limit_amount / free_float if limit_amount is not None and free_float and free_float > 0 else None
    open_num = number(item.get("open_num"))
    turnover = number(item.get("turnover_rate"))
    flags = ["post_close_only", "next_session_manual_review", "no_automatic_order"]
    if "tushare_limit_list_ths" not in sources:
        return {
            "model_version": MODEL_VERSION, "status": "unavailable", "eligible": False,
            "reason": "missing_tushare_limit_pool_fields", "streak_count": streak,
            "seal_to_float": seal_to_float, "risk_flags": [*flags, "missing_final_seal_or_float"],
        }
    if seal_to_float is None:
        return {
            "model_version": MODEL_VERSION, "status": "unavailable", "eligible": False,
            "reason": "missing_final_seal_or_float", "streak_count": streak,
            "seal_to_float": None, "risk_flags": [*flags, "missing_final_seal_or_float"],
        }
    if open_num is None:
        flags.append("open_board_count_missing_not_gated")
    elif open_num > 8:
        flags.append("many_open_boards")
    if turnover is not None and turnover < 5:
        flags.append("tight_board_next_session_fill_risk")
    if str(item.get("status") or "") == "一字板":
        flags.append("one_word_board_not_entry")
    eligible = streak >= MIN_BOARD_COUNT and seal_to_float >= MIN_SEAL_TO_FLOAT
    return {
        "model_version": MODEL_VERSION,
        "status": "candidate" if eligible else "filtered",
        "eligible": eligible,
        "signal_key": f"{symbol}:watch:{MODEL_VERSION}" if eligible and symbol else None,
        "streak_count": streak,
        "seal_to_float": round(seal_to_float, 6),
        "thresholds": {"min_board_count": MIN_BOARD_COUNT, "min_seal_to_float": MIN_SEAL_TO_FLOAT},
        "reason": "multi_board_with_final_seal_strength" if eligible else "does_not_meet_ladder_and_seal_screen",
        "risk_flags": flags,
        "interpretation": (
            "盘后封板与梯队组合仅用于下一交易日人工复核；开盘仍须检查可交易性、集合竞价、分钟承接和板块状态。"
        ),
    }


def rank_continuation_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank only eligible research watches with stable, explainable keys."""
    eligible = [item for item in items if bool((item.get("continuation_watch") or {}).get("eligible"))]
    eligible.sort(
        key=lambda item: (
            -float((item.get("continuation_watch") or {}).get("seal_to_float") or 0),
            -int((item.get("continuation_watch") or {}).get("streak_count") or 0),
            str(item.get("ts_code") or item.get("symbol") or ""),
        )
    )
    return [{**item, "continuation_watch": {**dict(item["continuation_watch"]), "rank": rank}}
            for rank, item in enumerate(eligible, start=1)]


__all__ = [
    "MIN_BOARD_COUNT", "MIN_SEAL_TO_FLOAT", "MODEL_VERSION", "continuation_watch", "rank_continuation_candidates",
]
