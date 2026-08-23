"""Bounded persisted inputs for post-close limit-pattern sample selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


@dataclass(frozen=True)
class StrategyPatternSampleInputs:
    limit_rows: list[dict[str, Any]]
    step_rows: list[dict[str, Any]]
    prior_limit_rows: list[dict[str, Any]]
    daily_rows: list[dict[str, Any]]


def load_strategy_pattern_sample_inputs(database: Any, as_of_date: date) -> StrategyPatternSampleInputs:
    """Read only persisted same-date ladder inputs and a bounded daily window.

    This repository deliberately does not refresh Tushare, minute bars, board
    membership, or LHB evidence.  The caller owns those separate local
    projections and passes all inputs to the deterministic selector.
    """
    stamp = as_of_date.strftime("%Y%m%d")
    with database.transaction() as connection:
        limit_rows = connection.execute(
            """SELECT DISTINCT ON(row_data->>'ts_code') row_data,provider_key,available_at
                 FROM quant.tushare_raw_records WHERE api_name='limit_list_ths'
                  AND row_data->>'trade_date'=%s AND row_data->>'limit_type'='涨停池'
                ORDER BY row_data->>'ts_code',available_at DESC""", (stamp,),
        ).fetchall()
        step_rows = connection.execute(
            """SELECT DISTINCT ON(row_data->>'ts_code') row_data,available_at
                 FROM quant.tushare_raw_records WHERE api_name='limit_step' AND row_data->>'trade_date'=%s
                ORDER BY row_data->>'ts_code',available_at DESC""", (stamp,),
        ).fetchall()
        prior_date_row = connection.execute(
            """SELECT max(row_data->>'trade_date') prior_date FROM quant.tushare_raw_records
                WHERE api_name='limit_list_ths' AND row_data->>'trade_date'<%s""", (stamp,),
        ).fetchone()
        prior_stamp = prior_date_row["prior_date"] if prior_date_row else None
        prior_limit_rows = connection.execute(
            """SELECT DISTINCT ON(row_data->>'ts_code') row_data
                 FROM quant.tushare_raw_records WHERE api_name='limit_list_ths'
                  AND row_data->>'trade_date'=%s AND row_data->>'limit_type'='涨停池'
                ORDER BY row_data->>'ts_code',available_at DESC""", (prior_stamp,),
        ).fetchall() if prior_stamp else []
        symbols = [str(row["row_data"].get("ts_code") or "").upper() for row in limit_rows]
        daily_rows = connection.execute(
            """WITH ranked AS (
                   SELECT b.*,row_number() OVER(PARTITION BY b.symbol ORDER BY b.trading_date DESC) rn
                     FROM quant.canonical_bars_daily b WHERE b.symbol=ANY(%s)
                      AND b.trading_date<=%s AND b.trading_date>=%s
                 ) SELECT * FROM ranked WHERE rn<=21 ORDER BY symbol,trading_date""",
            (symbols, as_of_date, as_of_date - timedelta(days=60)),
        ).fetchall() if symbols else []
    return StrategyPatternSampleInputs(
        limit_rows=[dict(row) for row in limit_rows],
        step_rows=[dict(row["row_data"] or {}) for row in step_rows],
        prior_limit_rows=[dict(row["row_data"] or {}) for row in prior_limit_rows],
        daily_rows=[dict(row) for row in daily_rows],
    )


__all__ = ["StrategyPatternSampleInputs", "load_strategy_pattern_sample_inputs"]
