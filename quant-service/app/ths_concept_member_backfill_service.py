"""Fail-closed orchestration for one bounded THS concept-member batch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class ThsConceptMemberBackfillDependencies:
    china_today: Callable[[], date]
    load_existing_flow: Callable[[date], Awaitable[Any]]
    sync_flow_catalog: Callable[[Any], Awaitable[dict[str, Any]]]
    flow_request: Callable[..., Any]
    sync_members: Callable[[Any], Awaitable[dict[str, Any]]]
    member_request: Callable[..., Any]
    load_progress: Callable[[date], Awaitable[Any]]


async def run(request: Any, dependencies: ThsConceptMemberBackfillDependencies) -> dict[str, Any]:
    """Confirm the daily flow universe before resuming exact memberships."""
    trade_date = request.trade_date or dependencies.china_today()
    existing = await dependencies.load_existing_flow(trade_date)
    refreshed: dict[str, Any] | None = None
    if request.refresh_flow_catalog or not int(existing["rows"]):
        refreshed = await dependencies.sync_flow_catalog(dependencies.flow_request(
            trade_date=trade_date, provider=request.provider,
        ))
        flow_status = (refreshed.get("sources", {}).get("concept_flow", {}) or {}).get("status")
        if flow_status not in {"completed", "partial", "unchanged", "empty"}:
            return {
                "status": "blocked", "trade_date": str(trade_date), "refresh": refreshed,
                "reason": "THS concept flow is unavailable; member mapping was not guessed",
            }
    result = await dependencies.sync_members(dependencies.member_request(
        trade_date=trade_date, provider=request.provider, member_limit=request.batch_size, resume=True,
    ))
    if result.get("status") == "blocked":
        return {
            **result, "trade_date": str(trade_date), "refresh": refreshed,
            "progress": {"completed_or_empty": 0, "failed": 0, "remaining": None},
        }
    progress = await dependencies.load_progress(trade_date)
    return {
        **result, "refresh": refreshed,
        "progress": {
            "completed_or_empty": int(progress["done"]), "failed": int(progress["failed"]),
            "remaining": max(0, int(result["total_concepts"]) - int(progress["done"])),
        },
    }


__all__ = ["ThsConceptMemberBackfillDependencies", "run"]
