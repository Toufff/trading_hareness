"""Dependency-injected concept-flow to limit-up stock research orchestration.

The service deliberately keeps the exact-membership and same-day join in the
existing sector/candidate services.  This module only coordinates their
bounded outputs with announcement and stock-study enrichment; it does not add
provider calls or turn research candidates into executable decisions.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Awaitable, Callable

from .request_models import (
    AnnouncementSyncRequest,
    BoardResearchRunRequest,
    ConceptCandidateSyncRequest,
    SectorFlowSyncRequest,
    StockStudyRequest,
)


async def run(
    request: BoardResearchRunRequest,
    *,
    database: Any,
    run_database: Callable[..., Awaitable[Any]],
    sync_concept_signals: Callable[[SectorFlowSyncRequest], Awaitable[dict[str, Any]]],
    sync_concept_limit_candidates: Callable[[ConceptCandidateSyncRequest], Awaitable[dict[str, Any]]],
    sync_announcements: Callable[[AnnouncementSyncRequest], Awaitable[dict[str, Any]]],
    build_stock_study: Callable[[str, StockStudyRequest], Awaitable[dict[str, Any]]],
    date_for: Callable[[str], date],
) -> dict[str, Any]:
    """Run one bounded board research pass with explicit dependencies."""
    signal_result = await sync_concept_signals(SectorFlowSyncRequest(
        trade_date=request.trade_date,
        provider=request.provider,
    ))
    candidate_result = await sync_concept_limit_candidates(ConceptCandidateSyncRequest(
        trade_date=request.trade_date,
        provider=request.provider,
        top_concepts=request.top_concepts,
        leaders_per_concept=request.leaders_per_concept,
    ))
    selected_date = candidate_result.get("trade_date")
    studies: list[dict[str, Any]] = []
    announcements: dict[str, Any] | None = None
    concept_keys = [
        str(item["sector_key"])
        for item in candidate_result.get("concepts", [])
        if item.get("sector_key")
    ]

    def load_candidates() -> list[Any]:
        if not selected_date or not concept_keys:
            return []
        with database.transaction() as connection:
            return connection.execute(
                """SELECT c.symbol,c.name,c.sector_key,s.label concept_label,c.limit_tag,c.limit_amount,
                          flow.net_amount board_net_amount
                     FROM quant.sector_limit_candidates c
                     JOIN quant.sectors s ON s.taxonomy_key=c.taxonomy_key AND s.sector_key=c.sector_key
                LEFT JOIN quant.sector_market_observations flow ON flow.taxonomy_key='ths_concept_flow' AND flow.sector_key=c.sector_key
                          AND flow.trading_date=c.trading_date
                    WHERE c.taxonomy_key='ths_concept_flow' AND c.trading_date=%s AND c.sector_key = ANY(%s)
                    ORDER BY flow.net_amount DESC NULLS LAST,c.limit_amount DESC NULLS LAST,c.symbol
                    LIMIT %s""",
                (selected_date, concept_keys, request.max_stock_studies * 2),
            ).fetchall()

    rows = await run_database(load_candidates)
    unique_rows: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for row in rows:
        symbol = str(row["symbol"])
        if symbol in seen_symbols:
            continue
        unique_rows.append(dict(row))
        seen_symbols.add(symbol)
        if len(unique_rows) >= request.max_stock_studies:
            break

    symbols = [str(row["symbol"]) for row in unique_rows]
    if request.sync_announcements and symbols and selected_date:
        announcement_end = date_for(str(selected_date))
        announcements = await sync_announcements(AnnouncementSyncRequest(
            symbols=symbols,
            start_date=announcement_end - timedelta(days=45),
            end_date=announcement_end,
            max_pages_per_symbol=1,
        ))

    for row in unique_rows:
        study = await build_stock_study(str(row["symbol"]), StockStudyRequest(
            as_of_date=date_for(str(selected_date)) if selected_date else None,
            lookback_days=request.study_lookback_days,
        ))
        studies.append({"candidate": dict(row), "study": {
            "symbol": study["symbol"],
            "as_of_date": study["as_of_date"],
            "technical": study["technical"],
            "analyst": study["analyst"]["summary"],
            "combined": study["combined"],
            "sources": study["sources"],
            "announcements": study.get("events", {}).get("announcements", []),
        }})

    failed_sources = [
        source
        for item in studies
        for source in item["study"]["sources"]
        if source.get("status") == "failed"
    ]
    status = "partial" if candidate_result.get("status") == "partial" or failed_sources else "completed"
    return {
        "status": status,
        "trade_date": selected_date,
        "signals": signal_result,
        "candidates": candidate_result,
        "announcements": announcements,
        "studies": studies,
        "decision_eligible": False,
        "notice": "板块到个股链路用于研究扫描；免费公告源只作事件补充。",
    }


__all__ = ["run"]
