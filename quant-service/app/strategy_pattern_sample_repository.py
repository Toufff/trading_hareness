"""Bounded persisted inputs for post-close limit-pattern sample selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
from typing import Any

from psycopg.types.json import Json


@dataclass(frozen=True)
class StrategyPatternSampleInputs:
    limit_rows: list[dict[str, Any]]
    step_rows: list[dict[str, Any]]
    prior_limit_rows: list[dict[str, Any]]
    daily_rows: list[dict[str, Any]]


def _event_limit_rows(connection: Any, as_of_date: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Project generic persisted limit events into the legacy sample shape."""
    current = connection.execute(
        """SELECT DISTINCT ON(symbol) symbol,body,source,available_at
             FROM quant.market_events
            WHERE event_type='limit_up_pool'
              AND (occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s
            ORDER BY symbol,available_at DESC""",
        (as_of_date,),
    ).fetchall()
    prior_row = connection.execute(
        """SELECT max((occurred_at AT TIME ZONE 'Asia/Shanghai')::date) AS prior_date
             FROM quant.market_events
            WHERE event_type='limit_up_pool'
              AND (occurred_at AT TIME ZONE 'Asia/Shanghai')::date<%s""",
        (as_of_date,),
    ).fetchone()
    prior_date = prior_row["prior_date"] if prior_row else None
    prior = connection.execute(
        """SELECT DISTINCT ON(symbol) symbol,body,source,available_at
             FROM quant.market_events
            WHERE event_type='limit_up_pool'
              AND (occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s
            ORDER BY symbol,available_at DESC""",
        (prior_date,),
    ).fetchall() if prior_date else []

    def project(row: dict[str, Any], day: date) -> dict[str, Any]:
        try:
            raw = json.loads(row.get("body") or "{}")
        except (TypeError, ValueError):
            raw = {}
        return {
            **raw,
            "ts_code": str(raw.get("ts_code") or row["symbol"]).upper(),
            "trade_date": day.strftime("%Y%m%d"),
            "limit_type": "涨停池",
            "status": raw.get("status") or "收盘封板",
            "provider_key": row.get("source"),
        }

    current_projected = [project(dict(row), as_of_date) for row in current]
    prior_projected = [project(dict(row), prior_date) for row in prior] if prior_date else []
    prior_symbols = {row["ts_code"] for row in prior_projected}
    step_rows = [
        {"ts_code": row["ts_code"], "trade_date": row["trade_date"],
         "nums": 2 if row["ts_code"] in prior_symbols else 1,
         "derivation": "persisted_close_limit_intersection"}
        for row in current_projected
    ]
    wrapped = [
        {"row_data": row, "provider_key": row.get("provider_key"), "available_at": None}
        for row in current_projected
    ]
    return wrapped, step_rows, prior_projected


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
        if not limit_rows:
            limit_rows, step_rows, prior_limit_rows = _event_limit_rows(connection, as_of_date)
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
        step_rows=[dict(row.get("row_data") or row) for row in step_rows],
        prior_limit_rows=[dict(row.get("row_data") or row) for row in prior_limit_rows],
        daily_rows=[dict(row) for row in daily_rows],
    )


def persist_strategy_pattern_run(
    database: Any,
    run_key: str,
    as_of_date: date,
    status: str,
    source_status: dict[str, Any],
    summary: dict[str, Any],
    samples: list[dict[str, Any]],
    *,
    model_version: str,
    json_safe: Any,
) -> Any:
    """Replace one bounded post-close pattern run atomically.

    Inputs are already selected and minute-replayed by the caller.  This
    repository never fetches providers or ranks samples, so persistence cannot
    accidentally widen the replay/data-collection boundary.
    """
    with database.transaction() as connection:
        run = connection.execute(
            """INSERT INTO quant.strategy_pattern_runs(run_key,as_of_date,model_version,status,source_status,summary)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(run_key) DO UPDATE SET status=EXCLUDED.status,source_status=EXCLUDED.source_status,
                 summary=EXCLUDED.summary,updated_at=now() RETURNING run_id""",
            (run_key, as_of_date, model_version, status,
             Json(json_safe(source_status)), Json(json_safe(summary))),
        ).fetchone()
        connection.execute("DELETE FROM quant.strategy_pattern_samples WHERE run_id=%s", (run["run_id"],))
        for rank, sample in enumerate(samples, start=1):
            connection.execute(
                """INSERT INTO quant.strategy_pattern_samples(run_id,rank,symbol,name,primary_cohort,cohorts,board_context,
                       limit_context,daily_features,intraday_pattern,minute_source,risk_flags)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run["run_id"], rank, sample["symbol"], sample.get("name"), sample["primary_cohort"], Json(sample["cohorts"]),
                 Json(json_safe(sample["board_context"])), Json(json_safe(sample["limit_context"])),
                 Json(json_safe(sample["daily_features"])), Json(json_safe(sample["intraday_pattern"])),
                 sample.get("minute_source"), Json(sample["risk_flags"])),
            )
    return run["run_id"]


__all__ = [
    "StrategyPatternSampleInputs", "load_strategy_pattern_sample_inputs", "persist_strategy_pattern_run",
]
