"""Persisted post-close evidence reads for strategy and replay services.

This repository deliberately owns only the bounded database projections.  The
aggregation rules remain in :mod:`post_close_evidence`, keeping source
provenance and same-date joins explicit at the composition boundary.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .sector_membership_repository import point_in_time_membership_predicate


def load_exact_board_context_rows(database: Any, as_of_date: date) -> list[dict[str, Any]]:
    """Return same-date exact board membership joined to saved board flow.

    The preferred path remains point-in-time THS concept membership.  The
    Longhu full-market close source supplies an exact THS industry ``plate_id``
    on every saved stock-flow row, so it is a valid same-date fallback rather
    than the former ``no_exact_ths_concept_mapping`` dead end.  Both branches
    are persisted evidence reads; this function never calls a provider.
    """
    membership_predicate = point_in_time_membership_predicate("member")
    with database.transaction() as connection:
        rows = connection.execute(
            f"""WITH exact_concepts AS (
                   SELECT member.symbol,flow.sector_key,sector.label,flow.net_amount,flow.change_pct,
                          flow.leading_label,flow.provider_key,flow.available_at,'ths_concept_flow'::text AS taxonomy_key
                     FROM quant.sector_membership_history member
                     JOIN quant.sector_market_observations flow
                       ON flow.taxonomy_key=member.taxonomy_key AND flow.sector_key=member.sector_key
                     JOIN quant.sectors sector
                       ON sector.taxonomy_key=flow.taxonomy_key AND sector.sector_key=flow.sector_key
                    WHERE member.taxonomy_key='ths_concept_flow' AND {membership_predicate}
                      AND flow.taxonomy_key='ths_concept_flow' AND flow.trading_date=%s
               ), latest_longhu_report AS (
                   SELECT observed_at,payload
                     FROM quant.intraday_board_reports
                    WHERE status='completed'
                      AND (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s
                      AND source_status->>'provider'='longhuvip_composite'
                    ORDER BY observed_at DESC LIMIT 1
               ), longhu_boards AS (
                   SELECT item->>'sector_key' AS sector_key,item->>'label' AS label,
                          nullif(item->>'net_inflow','')::numeric AS net_amount,
                          nullif(item->>'change_pct','')::numeric AS change_pct,
                          item#>>'{{top_stocks,0,name}}' AS leading_label,
                          'longhuvip_composite'::text AS provider_key,report.observed_at AS available_at,
                          'longhu_ths_industry'::text AS taxonomy_key
                     FROM latest_longhu_report report
                     CROSS JOIN LATERAL jsonb_array_elements(coalesce(report.payload->'items','[]'::jsonb)) item
               ), exact_longhu_industry AS (
                   SELECT stock.symbol,board.sector_key,board.label,board.net_amount,board.change_pct,
                          board.leading_label,board.provider_key,board.available_at,board.taxonomy_key
                     FROM quant.stock_money_flow_daily stock
                     JOIN longhu_boards board ON board.sector_key=stock.raw->>'plate_id'
                    WHERE stock.trading_date=%s AND stock.provider='longhuvip_composite'
                      AND stock.source='longhuvip_main_net'
               )
               SELECT * FROM exact_concepts
               UNION ALL
               SELECT longhu.* FROM exact_longhu_industry longhu
                WHERE NOT EXISTS (
                    SELECT 1 FROM exact_concepts concept WHERE concept.symbol=longhu.symbol
                )""",
            (as_of_date, as_of_date, as_of_date, as_of_date, as_of_date, as_of_date),
        ).fetchall()
    return [dict(row) for row in rows]


def load_tushare_lhb_context_rows(database: Any, as_of_date: date) -> list[dict[str, Any]]:
    """Return newest same-date Tushare top-list and institution-seat records."""
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT api_name,row_data,provider_key,available_at
                 FROM quant.tushare_raw_records
                WHERE api_name IN ('top_list','top_inst') AND row_data->>'trade_date'=%s
                ORDER BY available_at DESC""",
            (as_of_date.strftime("%Y%m%d"),),
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = ["load_exact_board_context_rows", "load_tushare_lhb_context_rows"]
