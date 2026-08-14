"""Bounded live-flow-driven hydration of exact Eastmoney board members."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


async def hydrate(
    kind: str,
    flows: list[dict[str, Any]],
    limit: int,
    *,
    run_database_blocking: Callable[..., Awaitable[Any]],
    run_public_blocking: Callable[..., Awaitable[Any]],
    board_members: Callable[..., Any],
    upsert_taxonomy: Callable[..., Any],
    upsert_sector: Callable[..., Any],
    persist_members: Callable[..., int],
    db: Any,
    intraday_number: Callable[[Any], float | None],
    executor_saturated_error: type[Exception],
    provider_error: type[Exception],
    safe_error_detail: Callable[[str, int], str],
) -> list[dict[str, Any]]:
    if not limit:
        return []
    taxonomy_key = f"eastmoney_{kind}"
    boards = []
    for flow in flows:
        label = str(flow.get("行业") or flow.get("板块名称") or "").strip()
        sector_key = str(flow.get("行业代码") or flow.get("板块代码") or label).strip()
        if label and sector_key:
            boards.append((sector_key, label, flow))
    if not boards:
        return []

    def load_mapped_rows() -> list[Any]:
        with db.transaction() as connection:
            return connection.execute(
                """SELECT DISTINCT sector_key FROM quant.sector_membership_history
                     WHERE taxonomy_key=%s AND effective_to IS NULL""", (taxonomy_key,),
            ).fetchall()
    mapped = {str(row["sector_key"]) for row in await run_database_blocking(load_mapped_rows)}

    def flow_priority(item: tuple[str, str, dict[str, Any]]) -> float:
        inflow, outflow = intraday_number(item[2].get("流入资金")), intraday_number(item[2].get("流出资金"))
        net = inflow - outflow if inflow is not None and outflow is not None else intraday_number(item[2].get("净额"))
        return -(net if net is not None else -float("inf"))

    selected = sorted((item for item in boards if item[0] not in mapped), key=flow_priority)[:limit]
    observed_at = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    for sector_key, label, _ in selected:
        try:
            rows = await run_public_blocking(board_members, kind, label, timeout_seconds=12)
            if not rows:
                results.append({"sector_key": sector_key, "label": label, "status": "empty", "members": 0})
                continue
            def persist_members() -> int:
                with db.transaction() as connection:
                    upsert_taxonomy(connection, taxonomy_key, f"东方财富{'概念' if kind == 'concept' else '行业'}板块", "akshare",
                                    {"source": "eastmoney", "kind": kind, "member_endpoint": "akshare_live_flow"})
                    upsert_sector(connection, taxonomy_key, sector_key, label, {"source": "eastmoney_live_flow", "label": label})
                    return persist_members(connection, taxonomy_key, sector_key, rows, observed_at)
            stored = await run_database_blocking(persist_members)
            results.append({"sector_key": sector_key, "label": label, "status": "completed", "members": stored})
        except executor_saturated_error as error:
            results.append({"sector_key": sector_key, "label": label, "status": "blocked", "members": 0, "error": safe_error_detail(str(error), 300)})
        except (asyncio.TimeoutError, provider_error, ValueError) as error:
            results.append({"sector_key": sector_key, "label": label, "status": "failed", "members": 0, "error": str(error)[:300]})
    return results


__all__ = ["hydrate"]
