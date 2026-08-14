"""Dependency-injected bounded THS catalog/member synchronization."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo


async def sync(
    request: Any,
    *,
    taxonomy_key: Callable[[str], str],
    fetch_catalog: Callable[[Any], Awaitable[dict[str, Any]]],
    catalog_request: Any,
    load_rows: Callable[[str], Awaitable[list[dict[str, Any]]]],
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    upsert_taxonomy: Callable[..., Any],
    upsert_sector: Callable[..., Any],
    ths_member_persist: Callable[..., int],
    member_sync_failure: Callable[..., Awaitable[Any]],
    is_local_capacity_error: Callable[[Exception], bool],
    is_circuit_open_error: Callable[[Exception], bool],
    http_exception: type[Exception],
    observed_at: Callable[[], datetime],
) -> dict[str, Any]:
    key = taxonomy_key(request.index_type)
    outcome = await fetch_catalog(catalog_request(
        api_name="ths_index", provider="super", params={"exchange": "A", "type": request.index_type}, max_rows=3000,
        paginate=True, page_size=1000, require_complete=True,
    ))
    rows = await load_rows(str(outcome["request_key"]))
    valid_rows = [row for row in rows if str(row.get("ts_code") or "").endswith(".TI") and row.get("name")]
    if not valid_rows:
        return {"status": "blocked", "taxonomy_key": key, "reason": "ths_index returned no valid board rows", "request_key": outcome["request_key"]}
    provider_key = str(outcome["provider"])
    observed_at_value = observed_at()

    def persist_catalog() -> None:
        with db.transaction() as connection:
            upsert_taxonomy(connection, key, f"同花顺 {request.index_type} 类板块", provider_key,
                            {"api_name": "ths_index", "index_type": request.index_type})
            for row in valid_rows:
                upsert_sector(connection, key, str(row["ts_code"]), str(row["name"]), row)

    await run_database_blocking(persist_catalog)
    member_results: list[dict[str, Any]] = []
    member_code_rows = [row for row in valid_rows if re.fullmatch(r"\d{6}\.TI", str(row["ts_code"]))]
    if request.resume:
        def select_incomplete() -> list[dict[str, Any]]:
            with db.transaction() as connection:
                rows = connection.execute(
                    """SELECT sector_key,count(*)::int members FROM quant.sector_membership_history
                         WHERE taxonomy_key=%s AND effective_to IS NULL GROUP BY sector_key""", (key,),
                ).fetchall()
                return [dict(row) for row in rows]
        active = {str(row["sector_key"]) for row in await run_database_blocking(select_incomplete) if int(row["members"] or 0) > 0}
        selected = [row for row in sorted(member_code_rows, key=lambda row: str(row["ts_code"])) if str(row["ts_code"]) not in active][:request.member_limit]
    else:
        selected = sorted(member_code_rows, key=lambda row: str(row["ts_code"]))[request.member_offset:request.member_offset + request.member_limit]
    if request.sync_members:
        for sector in selected:
            sector_key = str(sector["ts_code"])
            try:
                member_outcome = await fetch_catalog(catalog_request(
                    api_name="ths_member", provider="super", params={"ts_code": sector_key}, max_rows=10_000,
                    paginate=True, page_size=1000, max_pages=10, require_complete=True,
                ))
                member_rows = await load_rows(str(member_outcome["request_key"]))
                member_provider = str(member_outcome["provider"])

                def persist_members() -> int:
                    with db.transaction() as connection:
                        stored = ths_member_persist(connection, key, sector_key, member_rows, member_provider, observed_at_value)
                        connection.execute(
                            """INSERT INTO quant.sector_member_sync_state(taxonomy_key,sector_key,trading_date,state,attempts,member_count,last_error,provider_key,updated_at)
                               VALUES(%s,%s,%s,%s,1,%s,null,%s,now())
                               ON CONFLICT(taxonomy_key,sector_key,trading_date) DO UPDATE SET state=EXCLUDED.state,
                                 attempts=quant.sector_member_sync_state.attempts+1,member_count=EXCLUDED.member_count,
                                 last_error=null,provider_key=EXCLUDED.provider_key,updated_at=now()""",
                            (key, sector_key, observed_at_value.astimezone(ZoneInfo("Asia/Shanghai")).date(), "completed" if stored else "empty", stored, member_provider),
                        )
                        return stored

                stored = await run_database_blocking(persist_members)
                member_results.append({"sector_key": sector_key, "label": sector["name"], "status": member_outcome["status"],
                                       "received": member_outcome.get("received", member_outcome.get("stored", 0)), "members": stored,
                                       "provider": member_provider})
            except http_exception as error:
                member_status = "blocked" if is_local_capacity_error(error) else "circuit_open" if is_circuit_open_error(error) else "failed"
                if member_status == "failed":
                    await member_sync_failure(key, sector_key, observed_at_value, str(getattr(error, "detail", error))[:300], "tushare_super_sdk")
                member_results.append({"sector_key": sector_key, "label": sector["name"], "status": member_status,
                                       "members": 0, "error": str(getattr(error, "detail", error))})
    successful = [item for item in member_results if item["status"] in {"completed", "unchanged", "empty"}]
    failed = [item for item in member_results if item["status"] == "failed"]
    blocked = [item for item in member_results if item["status"] in {"blocked", "circuit_open"}]
    status = "blocked" if blocked and not successful and not failed else "partial" if failed or blocked else "completed"
    skipped = sum(1 for row in valid_rows if not re.fullmatch(r"\d{6}\.TI", str(row["ts_code"])))
    return {"status": status, "taxonomy_key": key, "index_type": request.index_type, "sectors": len(valid_rows),
            "provider": provider_key, "request_key": outcome["request_key"], "member_offset": request.member_offset,
            "resume": request.resume, "member_results": member_results, "skipped_non_member_codes": skipped,
            "next_member_offset": request.member_offset + len(selected) if request.sync_members and request.member_offset + len(selected) < len(member_code_rows) else None}


__all__ = ["sync"]
