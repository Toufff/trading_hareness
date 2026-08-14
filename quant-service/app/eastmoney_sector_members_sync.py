"""Dependency-injected bounded Eastmoney sector-member synchronization."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo


async def sync(
    request: Any,
    *,
    board_catalog: Callable[..., Awaitable[list[dict[str, Any]]]],
    board_members: Callable[..., Awaitable[list[dict[str, Any]]]],
    run_public_blocking: Callable[..., Awaitable[Any]],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    upsert_taxonomy: Callable[..., Any],
    upsert_sector: Callable[..., Any],
    persist_members: Callable[..., int],
    record_failure: Callable[..., Awaitable[Any]],
    safe_error_detail: Callable[[str, int], str],
    executor_saturated_error: type[Exception],
    provider_error: type[Exception],
    observed_at: datetime,
) -> dict[str, Any]:
    taxonomy_key = f"eastmoney_{request.kind}"
    try:
        catalog = await run_public_blocking(board_catalog, request.kind, timeout_seconds=12)
    except (asyncio.TimeoutError, executor_saturated_error, provider_error, ValueError) as error:
        return {"status": "blocked", "taxonomy_key": taxonomy_key, "reason": safe_error_detail(str(error), 500)}
    boards = [
        (str(row.get("板块代码") or row.get("板块名称") or "").strip(), str(row.get("板块名称") or row.get("名称") or "").strip(), row)
        for row in catalog
    ]
    boards = sorted([item for item in boards if item[0] and item[1]], key=lambda item: item[0])
    if not boards:
        return {"status": "blocked", "taxonomy_key": taxonomy_key, "reason": "Eastmoney board directory returned no valid board keys"}

    def persist_catalog() -> None:
        with db.transaction() as connection:
            upsert_taxonomy(connection, taxonomy_key, f"东方财富{'概念' if request.kind == 'concept' else '行业'}板块", "akshare",
                            {"source": "eastmoney", "kind": request.kind, "member_endpoint": "akshare"})
            for sector_key, label, raw in boards:
                upsert_sector(connection, taxonomy_key, sector_key, label, raw)
    await run_database_blocking(persist_catalog)

    if request.resume:
        def select_incomplete() -> list[Any]:
            with db.transaction() as connection:
                active_rows = connection.execute(
                    """SELECT sector_key,count(*)::int members FROM quant.sector_membership_history
                         WHERE taxonomy_key=%s AND effective_to IS NULL GROUP BY sector_key""", (taxonomy_key,),
                ).fetchall()
                failed_rows = connection.execute(
                    """SELECT sector_key,attempts FROM quant.sector_member_sync_state
                         WHERE taxonomy_key=%s AND trading_date=%s AND state='failed'""",
                    (taxonomy_key, observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()),
                ).fetchall()
            active = {str(row["sector_key"]) for row in active_rows if int(row["members"] or 0) > 0}
            failed = {str(row["sector_key"]): int(row["attempts"] or 0) for row in failed_rows}
            return [board for board in boards if board[0] not in active and (board[0] not in failed or failed[board[0]] < 3)][:request.member_limit]
        selected = await run_database_blocking(select_incomplete)
    else:
        selected = boards[request.member_offset:request.member_offset + request.member_limit]

    results: list[dict[str, Any]] = []
    for sector_key, label, _ in selected:
        try:
            rows = await run_public_blocking(board_members, request.kind, label, timeout_seconds=12)
            if not rows:
                def persist_empty_state() -> None:
                    with db.transaction() as connection:
                        connection.execute(
                            """INSERT INTO quant.sector_member_sync_state(taxonomy_key,sector_key,trading_date,state,attempts,member_count,last_error,provider_key,updated_at)
                               VALUES(%s,%s,%s,'failed',1,0,%s,'akshare',now())
                               ON CONFLICT(taxonomy_key,sector_key,trading_date) DO UPDATE SET state='failed',
                                 attempts=quant.sector_member_sync_state.attempts+1,last_error=EXCLUDED.last_error,updated_at=now()""",
                            (taxonomy_key, sector_key, observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date(), "Eastmoney member response was empty"),
                        )
                await run_database_blocking(persist_empty_state)
                results.append({"sector_key": sector_key, "label": label, "status": "failed", "members": 0, "error": "Eastmoney member response was empty"})
                continue
            def persist_members() -> int:
                with db.transaction() as connection:
                    stored = persist_members(connection, taxonomy_key, sector_key, rows, observed_at)
                    state = "completed" if stored else "failed"
                    connection.execute(
                        """INSERT INTO quant.sector_member_sync_state(taxonomy_key,sector_key,trading_date,state,attempts,member_count,last_error,provider_key,updated_at)
                           VALUES(%s,%s,%s,%s,1,%s,%s,'akshare',now())
                           ON CONFLICT(taxonomy_key,sector_key,trading_date) DO UPDATE SET state=EXCLUDED.state,
                             attempts=quant.sector_member_sync_state.attempts+1,member_count=EXCLUDED.member_count,
                             last_error=EXCLUDED.last_error,provider_key=EXCLUDED.provider_key,updated_at=now()""",
                        (taxonomy_key, sector_key, observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date(), state, stored,
                         None if stored else "Eastmoney response contained no recognizable A-share members"),
                    )
                    return stored
            stored = await run_database_blocking(persist_members)
            results.append({"sector_key": sector_key, "label": label, "status": "completed", "members": stored})
        except executor_saturated_error as error:
            await record_failure(taxonomy_key, sector_key, observed_at, safe_error_detail(str(error), 300), "akshare")
            results.append({"sector_key": sector_key, "label": label, "status": "blocked", "members": 0, "error": safe_error_detail(str(error), 300)})
        except (asyncio.TimeoutError, provider_error, ValueError) as error:
            await record_failure(taxonomy_key, sector_key, observed_at, str(error)[:300], "akshare")
            results.append({"sector_key": sector_key, "label": label, "status": "failed", "members": 0, "error": str(error)[:300]})
    failures = [item for item in results if item["status"] not in {"completed", "empty"}]
    next_offset = request.member_offset + len(selected)
    return {"status": "partial" if failures else "completed", "taxonomy_key": taxonomy_key, "kind": request.kind,
            "total_boards": len(boards), "member_offset": request.member_offset, "member_limit": request.member_limit,
            "resume": request.resume, "member_results": results, "next_member_offset": next_offset if next_offset < len(boards) else None}


__all__ = ["sync"]
