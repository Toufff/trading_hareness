"""Bounded, exact-code concept-to-limit-up candidate orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any


@dataclass(frozen=True)
class ConceptLimitCandidateDependencies:
    select_concepts: Callable[[date | None, int], Awaitable[tuple[date | None, list[Any]]]]
    now_utc: Callable[[], datetime]
    fetch_catalog: Callable[[Any], Awaitable[dict[str, Any]]]
    request: Callable[..., Any]
    load_rows: Callable[[str], Awaitable[list[dict[str, Any]]]]
    persist_members: Callable[[str, list[dict[str, Any]], str, datetime], Awaitable[int]]
    persist_candidates: Callable[..., Awaitable[tuple[int, list[dict[str, Any]]]]]
    http_exception: type[Exception]


async def run(request: Any, dependencies: ConceptLimitCandidateDependencies) -> dict[str, Any]:
    """Build candidates only from same-date, exact ``ths_member`` codes."""
    if request.provider == "super_get":
        return {"status": "blocked", "reason": "complete ths_member snapshots require provider=super, super_sdk, or auto"}
    selected_date, concepts = await dependencies.select_concepts(request.trade_date, request.top_concepts)
    if selected_date is None:
        return {"status": "blocked", "reason": "sync concept flow before building limit-up candidates"}
    if not concepts:
        return {"status": "blocked", "trade_date": str(selected_date), "reason": "no positive concept-flow cross-section available"}

    observed_at = dependencies.now_utc()
    member_results: list[dict[str, Any]] = []
    concept_keys = [str(item["sector_key"]) for item in concepts]
    for concept in concepts:
        sector_key = str(concept["sector_key"])
        try:
            outcome = await dependencies.fetch_catalog(dependencies.request(
                api_name="ths_member", provider=request.provider, params={"ts_code": sector_key}, max_rows=10_000,
                paginate=True, page_size=1000, max_pages=10, require_complete=True,
            ))
            rows = await dependencies.load_rows(str(outcome["request_key"]))
            member_provider = str(outcome["provider"])
            stored = await dependencies.persist_members(sector_key, rows, member_provider, observed_at)
            member_results.append({
                "sector_key": sector_key, "label": concept["label"], "status": outcome["status"],
                "members": stored, "provider": outcome["provider"],
            })
        except dependencies.http_exception as error:
            detail = getattr(error, "detail", str(error))
            member_results.append({
                "sector_key": sector_key, "label": concept["label"], "status": "failed",
                "members": 0, "error": str(detail),
            })

    try:
        limit_outcome = await dependencies.fetch_catalog(dependencies.request(
            api_name="limit_list_ths", provider=request.provider,
            params={"trade_date": selected_date.strftime("%Y%m%d")}, max_rows=3000,
        ))
    except dependencies.http_exception as error:
        detail = getattr(error, "detail", str(error))
        return {
            "status": "partial", "trade_date": str(selected_date), "member_results": member_results,
            "reason": f"limit_list_ths failed: {detail}",
        }
    limit_provider = str(limit_outcome["provider"])
    limit_rows = await dependencies.load_rows(str(limit_outcome["request_key"]))
    limit_by_symbol = {
        str(row.get("ts_code") or "").upper(): row
        for row in limit_rows
        if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row.get("ts_code") or "").upper())
        and row.get("limit_type") == "涨停池"
    }
    membership_status = {str(item["sector_key"]): str(item["status"]) for item in member_results}
    stored, per_concept = await dependencies.persist_candidates(
        selected_date, concepts, concept_keys, limit_provider, limit_by_symbol,
        membership_status, observed_at, request.leaders_per_concept,
    )
    failed_members = [item for item in member_results if item["status"] not in {"completed", "unchanged", "empty"}]
    return {
        "status": "partial" if failed_members else "completed", "trade_date": str(selected_date),
        "concepts": per_concept, "member_results": member_results, "limit_provider": limit_provider,
        "limit_request_key": limit_outcome["request_key"], "limit_rows": len(limit_by_symbol), "candidates": stored,
    }


__all__ = ["ConceptLimitCandidateDependencies", "run"]
