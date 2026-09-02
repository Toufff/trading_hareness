"""Declarative ownership contracts for background research tasks.

The registry is intentionally metadata-only: factories remain in the
composition root, while profile ownership, evidence outputs and operational
expectations have one machine-readable source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True)
class RuntimeTaskContract:
    label: str
    owner_profile: str
    cadence: str
    provider_capabilities: tuple[str, ...]
    evidence_datasets: tuple[str, ...]
    description: str
    freshness_budget_seconds: float | None = None


RUNTIME_TASK_CONTRACTS: Final[dict[str, RuntimeTaskContract]] = {
    "intraday_monitor": RuntimeTaskContract(
        "intraday_monitor", "intraday_edge", "30s during session", ("order_book_quote", "a_share_prices_snapshot"),
        ("intraday_scan_runs", "intraday_signal_events"), "bounded watchlist scan and research alert writer", 90,
    ),
    "super_get_fast_quote": RuntimeTaskContract(
        "super_get_fast_quote", "intraday_edge", "1s rotating during session", ("rt_k",),
        ("intraday_fast_quotes",), "secondary same-session quote confirmation", 30,
    ),
    "minute_profile_capture": RuntimeTaskContract(
        "minute_profile_capture", "intraday_edge", "bounded market-session polling", ("intraday_minutes",),
        ("intraday_minute_sessions",), "minute features and time-of-day profile evidence", 180,
    ),
    "tencent_order_book": RuntimeTaskContract(
        "tencent_order_book", "intraday_edge", "3s during session", ("order_book_quote",),
        ("intraday_order_book_observations",), "bounded watchlist order-book capture", 20,
    ),
    "board_flow_curve": RuntimeTaskContract(
        "board_flow_curve", "intraday_edge", "market-session polling", ("board_flow",),
        ("intraday_board_flow_snapshots",), "board-flow curve evidence", 180,
    ),
    "market_event_capture": RuntimeTaskContract(
        "market_event_capture", "intraday_edge", "60s during market observation windows",
        ("a_share_auction_snapshot", "a_share_limit_up_pool", "a_share_limit_break_pool", "a_share_limit_up_ladder"),
        ("market_events",), "all-A auction, limit-pool and limit-chain evidence capture", 180,
    ),
    "all_a_level1_snapshot": RuntimeTaskContract(
        "all_a_level1_snapshot", "intraday_edge", "60s during market session",
        ("a_share_prices_snapshot",), ("raw_market_observations",),
        "complete all-A Level-1 raw snapshot for width/rank and validation windows", 120,
    ),
    "strategy_review": RuntimeTaskContract(
        "strategy_review", "research", "scheduled checkpoints", ("daily", "board_flow"),
        ("analyst_market_reviews", "strategy_reviews"), "post-close descriptive strategy review",
    ),
    "post_close_strategy": RuntimeTaskContract(
        "post_close_strategy", "research", "same-date post-close window", ("daily", "daily_basic"),
        ("strategy_candidates", "automation_runs"), "post-close research candidate generation",
    ),
    "ten_day_leader_rotation": RuntimeTaskContract(
        "ten_day_leader_rotation", "research", "scheduled post-close", ("daily", "limit_list_d"),
        ("ten_day_leader_rotation_runs",), "ten-day leader rotation research",
    ),
    "daily_strategy_summary": RuntimeTaskContract(
        "daily_strategy_summary", "research", "daily", (),
        ("strategy_day_summaries",), "research-only daily summary materialization",
    ),
    "ths_member_backfill": RuntimeTaskContract(
        "ths_member_backfill", "research", "bounded background batches", ("ths_member",),
        ("sector_membership_history",), "point-in-time THS constituent backfill",
    ),
    "all_board_member_backfill": RuntimeTaskContract(
        "all_board_member_backfill", "research", "bounded background batches", ("dc_member", "ths_member"),
        ("sector_membership_history",), "bounded board member coverage backfill",
    ),
    "retention_maintenance": RuntimeTaskContract(
        "retention_maintenance", "research", "daily after 16:30 CST", (),
        ("retention_policies",), "bounded batch delete for enabled retention policies",
    ),
}


def runtime_task_contract(label: str) -> RuntimeTaskContract:
    try:
        return RUNTIME_TASK_CONTRACTS[label]
    except KeyError as error:
        raise ValueError(f"unknown runtime task contract: {label}") from error


def runtime_profile_owns_task(profile: str, label: str) -> bool:
    """Return whether a declared runtime profile may acquire a task lease."""
    if profile == "full":
        return True
    return runtime_task_contract(label).owner_profile == profile


def runtime_task_contract_catalog() -> list[dict[str, Any]]:
    """Expose deterministic, secret-free runtime ownership to agents/UI."""
    return [
        {
            "label": item.label,
            "owner_profile": item.owner_profile,
            "cadence": item.cadence,
            "provider_capabilities": list(item.provider_capabilities),
            "evidence_datasets": list(item.evidence_datasets),
            "description": item.description,
            "freshness_budget_seconds": item.freshness_budget_seconds,
        }
        for item in sorted(RUNTIME_TASK_CONTRACTS.values(), key=lambda item: item.label)
    ]


def intraday_edge_task_labels() -> frozenset[str]:
    return frozenset(
        item.label for item in RUNTIME_TASK_CONTRACTS.values() if item.owner_profile == "intraday_edge"
    )


__all__ = [
    "RUNTIME_TASK_CONTRACTS", "RuntimeTaskContract", "intraday_edge_task_labels",
    "runtime_profile_owns_task", "runtime_task_contract", "runtime_task_contract_catalog",
]
