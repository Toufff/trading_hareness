"""Bounded orchestration for the documented THS sector taxonomies."""

from __future__ import annotations

from typing import Any, Awaitable, Callable


async def sync_all(
    *,
    sync_one: Callable[[Any], Awaitable[dict[str, Any]]],
    request_type: Any,
    http_exception: type[Exception],
    is_local_capacity_error: Callable[[Exception], bool],
    is_circuit_open_error: Callable[[Exception], bool],
) -> dict[str, Any]:
    """Refresh N/I/R/S/ST/BB sequentially, preserving bounded provider use."""
    results: list[dict[str, Any]] = []
    for index_type in ("N", "I", "R", "S", "ST", "BB"):
        try:
            results.append(await sync_one(request_type(index_type=index_type)))
        except http_exception as error:
            item_status = "blocked" if is_local_capacity_error(error) else "circuit_open" if is_circuit_open_error(error) else "failed"
            results.append({"status": item_status, "index_type": index_type, "reason": str(getattr(error, "detail", error)), "sectors": 0})
    successful = [item for item in results if item["status"] in {"completed", "unchanged"}]
    failed = [item for item in results if item["status"] == "failed"]
    blocked = [item for item in results if item["status"] in {"blocked", "circuit_open"}]
    status = "blocked" if blocked and not successful and not failed else "partial" if failed or blocked else "completed"
    return {"status": status, "types": results, "sectors": sum(int(item.get("sectors", 0)) for item in results)}


__all__ = ["sync_all"]
