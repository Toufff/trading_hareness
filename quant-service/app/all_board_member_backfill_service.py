"""Bounded orchestration for exact cross-source board-member coverage."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AllBoardMemberBackfillDependencies:
    sync_all_ths_catalogs: Callable[[], Awaitable[dict[str, Any]]]
    sync_ths_catalog: Callable[[Any], Awaitable[dict[str, Any]]]
    ths_request: Callable[..., Any]
    sync_eastmoney_members: Callable[[Any], Awaitable[dict[str, Any]]]
    eastmoney_request: Callable[..., Any]
    http_exception: type[Exception]


async def run(request: Any, dependencies: AllBoardMemberBackfillDependencies) -> dict[str, Any]:
    """Advance one bounded exact-member batch; never infer membership by name."""
    results: list[dict[str, Any]] = []
    if request.refresh_catalogs and request.include_ths:
        results.append({"source": "ths_catalogs", **await dependencies.sync_all_ths_catalogs()})
    if request.include_ths:
        for index_type in ("N", "I", "R", "S", "ST", "BB"):
            try:
                item = await dependencies.sync_ths_catalog(dependencies.ths_request(
                    index_type=index_type, sync_members=True, member_limit=request.batch_size, resume=True,
                ))
                results.append({"source": "ths_member", **item})
            except dependencies.http_exception as error:
                detail = getattr(error, "detail", str(error))
                results.append({
                    "source": "ths_member", "index_type": index_type,
                    "status": "failed", "reason": str(detail)[:300],
                })
    if request.include_eastmoney:
        for kind in ("industry", "concept"):
            item = await dependencies.sync_eastmoney_members(dependencies.eastmoney_request(
                kind=kind, member_limit=request.batch_size, resume=True,
            ))
            results.append({"source": "eastmoney_member", **item})
    successful = [item for item in results if item.get("status") in {"completed", "partial"}]
    failed = [item for item in results if item.get("status") in {"blocked", "failed"}]
    return {
        "status": "partial" if failed else "completed",
        "batch_size": request.batch_size,
        "results": results,
        "notice": "本次只推进受限批次；自动任务在盘后续跑，成员关系仅来自各源的精确代码/原始成员接口。",
        "successful_sources": len(successful),
        "failed_sources": len(failed),
    }


__all__ = ["AllBoardMemberBackfillDependencies", "run"]
