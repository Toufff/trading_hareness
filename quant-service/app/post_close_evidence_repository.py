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
    """Return only same-date, as-known-at THS concept-flow membership rows."""
    membership_predicate = point_in_time_membership_predicate("member")
    with database.transaction() as connection:
        rows = connection.execute(
            f"""SELECT member.symbol,flow.sector_key,sector.label,flow.net_amount,flow.change_pct,flow.leading_label,
                      flow.provider_key,flow.available_at
                 FROM quant.sector_membership_history member
                 JOIN quant.sector_market_observations flow
                   ON flow.taxonomy_key=member.taxonomy_key AND flow.sector_key=member.sector_key
                 JOIN quant.sectors sector ON sector.taxonomy_key=flow.taxonomy_key AND sector.sector_key=flow.sector_key
                WHERE member.taxonomy_key='ths_concept_flow' AND {membership_predicate}
                  AND flow.taxonomy_key='ths_concept_flow' AND flow.trading_date=%s""",
            (as_of_date, as_of_date, as_of_date, as_of_date),
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
