"""Bounded orchestration for exact limit-up linkage research candidates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class LimitLinkageMiningDependencies:
    trade_date: Callable[[datetime], date]
    load_relations: Callable[[date], Awaitable[list[dict[str, Any]]]]
    select_candidates: Callable[[list[dict[str, Any]], dict[str, dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any]]]
    persist: Callable[[datetime, date, list[dict[str, Any]], dict[str, Any]], Awaitable[str]]
    safe_error: Callable[[str, int], str]


async def run(
    observed_at: datetime,
    quote_by_symbol: dict[str, dict[str, Any]],
    dependencies: LimitLinkageMiningDependencies,
) -> dict[str, Any]:
    """Build and persist research-only peers without an extra quote request."""
    trade_date = dependencies.trade_date(observed_at)
    try:
        relations = await dependencies.load_relations(trade_date)
    except Exception as error:  # local evidence failure cannot fail a board report
        return {"status": "failed", "reason": dependencies.safe_error(str(error), 300), "summary": {"candidate_count": 0}}
    candidates, summary = dependencies.select_candidates(relations, quote_by_symbol)
    if not relations:
        return {
            "status": "blocked",
            "reason": "no same-date Eastmoney limit-up anchors with exact THS concept membership",
            "summary": summary,
        }
    try:
        run_id = await dependencies.persist(observed_at, trade_date, candidates, summary)
    except Exception as error:  # candidate evidence remains useful to the enclosing board report
        return {"status": "partial", "reason": dependencies.safe_error(str(error), 300), "summary": summary}
    return {"status": "completed", "linkage_run_id": run_id, "summary": summary, "candidates": candidates}


__all__ = ["LimitLinkageMiningDependencies", "run"]
