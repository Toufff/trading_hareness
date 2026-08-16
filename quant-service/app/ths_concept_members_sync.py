"""Dependency-injected bounded THS concept-member synchronization."""

from __future__ import annotations

from datetime import datetime, date
from typing import Any, Awaitable, Callable


# A member snapshot that failed only because every provider circuit was open is
# not a data-quality verdict.  Once the circuit cools down it must become
# eligible for one bounded, normal resume attempt; otherwise a transient
# outage permanently leaves a board outside the exact-membership universe.
# Other failures retain the three-attempt cap below so we do not turn malformed
# or capped responses into an unbounded retry loop.
TRANSIENT_CIRCUIT_OPEN_FAILURE_PREFIX = "all configured providers are temporarily circuit-open"


async def sync(
    request: Any,
    *,
    sync_flow_catalog: Callable[[Any], Awaitable[dict[str, Any]]],
    flow_request: Any,
    run_database_blocking: Callable[..., Awaitable[Any]],
    db: Any,
    fetch_catalog: Callable[[Any], Awaitable[dict[str, Any]]],
    catalog_request: Any,
    load_rows: Callable[[str], Awaitable[list[dict[str, Any]]]],
    persist_members: Callable[..., int],
    observed_at: Callable[[], datetime],
    http_exception: type[Exception],
) -> dict[str, Any]:
    if request.provider == "super_get":
        return {"status": "blocked", "reason": "complete ths_member snapshots require provider=super, super_sdk, or auto"}
    if request.refresh_flow_catalog:
        refreshed = await sync_flow_catalog(flow_request(trade_date=request.trade_date, provider=request.provider))
        if refreshed.get("sources", {}).get("concept_flow", {}).get("status") not in {"completed", "partial", "unchanged", "empty"}:
            return {"status": "blocked", "reason": "unable to refresh THS concept-flow catalog", "refresh": refreshed}

    def select_concepts() -> tuple[date | None, list[Any], int]:
        with db.transaction() as connection:
            selected_date = request.trade_date or connection.execute(
                "SELECT max(trading_date) latest FROM quant.sector_market_observations WHERE taxonomy_key='ths_concept_flow'"
            ).fetchone()["latest"]
            if selected_date is None:
                return None, [], 0
            if request.resume:
                concepts = connection.execute(
                    """SELECT o.sector_key,s.label
                         FROM quant.sector_market_observations o
                         JOIN quant.sectors s ON s.taxonomy_key=o.taxonomy_key AND s.sector_key=o.sector_key
                         LEFT JOIN quant.sector_member_sync_state state
                           ON state.taxonomy_key=o.taxonomy_key AND state.sector_key=o.sector_key AND state.trading_date=o.trading_date
                        WHERE o.taxonomy_key='ths_concept_flow' AND o.trading_date=%s
                          AND (state.state IS NULL OR (state.state='failed' AND
                               (state.attempts < 3 OR state.provider_key='tushare_super' OR
                                state.last_error='member response reached the 3000-row safety cap' OR
                                state.last_error LIKE %s)))
                        ORDER BY o.net_amount DESC NULLS LAST,o.sector_key LIMIT %s""",
                    (selected_date, f"{TRANSIENT_CIRCUIT_OPEN_FAILURE_PREFIX}%", request.member_limit),
                ).fetchall()
            else:
                concepts = connection.execute(
                    """SELECT o.sector_key,s.label
                         FROM quant.sector_market_observations o
                         JOIN quant.sectors s ON s.taxonomy_key=o.taxonomy_key AND s.sector_key=o.sector_key
                        WHERE o.taxonomy_key='ths_concept_flow' AND o.trading_date=%s
                        ORDER BY o.sector_key LIMIT %s OFFSET %s""",
                    (selected_date, request.member_limit, request.member_offset),
                ).fetchall()
            total = connection.execute(
                "SELECT count(*)::int total FROM quant.sector_market_observations WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s",
                (selected_date,),
            ).fetchone()["total"]
        return selected_date, concepts, total

    selected_date, concepts, total = await run_database_blocking(select_concepts)
    if selected_date is None:
        return {"status": "blocked", "reason": "sync THS concept flow before synchronizing concept members"}
    observed = observed_at()
    results: list[dict[str, Any]] = []
    for concept in concepts:
        sector_key, label = str(concept["sector_key"]), str(concept["label"])
        try:
            outcome = await fetch_catalog(catalog_request(
                api_name="ths_member", provider=request.provider, params={"ts_code": sector_key}, max_rows=10_000,
                paginate=True, page_size=1000, max_pages=10, require_complete=True,
            ))
            rows = await load_rows(str(outcome["request_key"]))
            provider_key = str(outcome["provider"])
            if outcome["status"] == "partial":
                def persist_capped_state() -> None:
                    with db.transaction() as connection:
                        connection.execute(
                            """INSERT INTO quant.sector_member_sync_state(taxonomy_key,sector_key,trading_date,state,attempts,member_count,last_error,provider_key,updated_at)
                               VALUES('ths_concept_flow',%s,%s,'failed',1,0,%s,%s,now())
                               ON CONFLICT(taxonomy_key,sector_key,trading_date) DO UPDATE SET state='failed',
                                 attempts=quant.sector_member_sync_state.attempts+1,last_error=EXCLUDED.last_error,
                                 provider_key=EXCLUDED.provider_key,updated_at=now()""",
                            (sector_key, selected_date, "member response reached the 3000-row safety cap", provider_key),
                        )
                await run_database_blocking(persist_capped_state)
                results.append({"sector_key": sector_key, "label": label, "status": "partial", "members": 0,
                                "received": len(rows), "provider": provider_key, "request_key": outcome["request_key"],
                                "reason": "member response reached the 3000-row safety cap"})
                continue
            def persist_member_snapshot() -> int:
                with db.transaction() as connection:
                    stored = persist_members(connection, "ths_concept_flow", sector_key, rows, provider_key, observed)
                    connection.execute(
                        """INSERT INTO quant.sector_member_sync_state(taxonomy_key,sector_key,trading_date,state,attempts,member_count,last_error,provider_key,updated_at)
                           VALUES('ths_concept_flow',%s,%s,%s,1,%s,null,%s,now())
                           ON CONFLICT(taxonomy_key,sector_key,trading_date) DO UPDATE SET state=EXCLUDED.state,
                             attempts=quant.sector_member_sync_state.attempts+1,member_count=EXCLUDED.member_count,
                             last_error=null,provider_key=EXCLUDED.provider_key,updated_at=now()""",
                        (sector_key, selected_date, "completed" if rows else "empty", stored, provider_key),
                    )
                    return stored
            stored = await run_database_blocking(persist_member_snapshot)
            results.append({"sector_key": sector_key, "label": label, "status": outcome["status"], "members": stored,
                            "provider": provider_key, "request_key": outcome["request_key"]})
        except http_exception as error:
            detail = str(getattr(error, "detail", error))[:500]
            def persist_failed_state() -> None:
                with db.transaction() as connection:
                    connection.execute(
                        """INSERT INTO quant.sector_member_sync_state(taxonomy_key,sector_key,trading_date,state,attempts,member_count,last_error,updated_at)
                           VALUES('ths_concept_flow',%s,%s,'failed',1,0,%s,now())
                           ON CONFLICT(taxonomy_key,sector_key,trading_date) DO UPDATE SET state='failed',
                             attempts=quant.sector_member_sync_state.attempts+1,last_error=EXCLUDED.last_error,updated_at=now()""",
                        (sector_key, selected_date, detail),
                    )
            await run_database_blocking(persist_failed_state)
            results.append({"sector_key": sector_key, "label": label, "status": "failed", "members": 0, "error": detail})
    failures = [item for item in results if item["status"] not in {"completed", "unchanged", "empty"}]
    next_offset = request.member_offset + len(concepts)
    return {"status": "partial" if failures else "completed", "trade_date": str(selected_date),
            "taxonomy_key": "ths_concept_flow", "total_concepts": total, "member_offset": request.member_offset,
            "member_limit": request.member_limit, "resume": request.resume, "member_results": results,
            "next_member_offset": next_offset if next_offset < total else None}


__all__ = ["TRANSIENT_CIRCUIT_OPEN_FAILURE_PREFIX", "sync"]
