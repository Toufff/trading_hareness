"""Native-async exact relation evidence for live limit-up linkage research."""

from __future__ import annotations

from datetime import date
from typing import Any


async def relations(async_database: Any, trade_date: date) -> list[dict[str, Any]]:
    """Return non-anchor peers sharing active, bounded THS concepts exactly."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """WITH anchors AS (
                   SELECT DISTINCT event.symbol,coalesce(instrument.name,event.symbol) AS name
                     FROM quant.market_events event
                LEFT JOIN quant.instruments instrument ON instrument.symbol=event.symbol
                    WHERE event.event_type='limit_up_pool'
                      AND (event.occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s
                 ), eligible_concepts AS (
                   SELECT sector_key
                     FROM quant.sector_membership_history
                    WHERE taxonomy_key='ths_concept_flow' AND effective_to IS NULL
                    GROUP BY sector_key
                   HAVING count(*) BETWEEN 2 AND 200
                 ), shared AS (
                   SELECT candidate.symbol,anchor.symbol AS leader_symbol,anchor.name AS leader_name,
                          leader.sector_key,sector.label
                     FROM anchors anchor
                     JOIN quant.sector_membership_history leader
                       ON leader.symbol=anchor.symbol AND leader.taxonomy_key='ths_concept_flow' AND leader.effective_to IS NULL
                     JOIN eligible_concepts eligible ON eligible.sector_key=leader.sector_key
                     JOIN quant.sector_membership_history candidate
                       ON candidate.taxonomy_key=leader.taxonomy_key AND candidate.sector_key=leader.sector_key
                      AND candidate.effective_to IS NULL AND candidate.symbol<>anchor.symbol
                      AND candidate.symbol NOT IN (SELECT symbol FROM anchors)
                     JOIN quant.sectors sector ON sector.taxonomy_key=leader.taxonomy_key AND sector.sector_key=leader.sector_key
                 )
                 SELECT symbol,array_agg(DISTINCT sector_key) AS concept_keys,array_agg(DISTINCT label) AS concept_labels,
                        array_agg(DISTINCT leader_symbol) AS leader_symbols,array_agg(DISTINCT leader_name) AS leader_names
                   FROM shared GROUP BY symbol""",
            (trade_date,),
        )
        rows = await result.fetchall()
    return [{**dict(row), "shared_concepts": len(row["concept_keys"] or [])} for row in rows]


__all__ = ["relations"]
