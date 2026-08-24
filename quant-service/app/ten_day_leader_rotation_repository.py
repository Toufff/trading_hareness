"""Database boundary for ten-day leader-rotation shadow materialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from psycopg.types.json import Json


@dataclass(frozen=True)
class TenDayRankingInputs:
    daily_rows: list[dict[str, Any]]
    daily_symbols: int
    strategy_available_at: datetime | None
    expected_daily_symbols: int = 0


def latest_full_market_date(database: Any, minimum_full_market_symbols: int) -> date | None:
    """Resolve the latest point-in-time all-A cross-section meeting both gates."""
    with database.transaction() as connection:
        row = connection.execute(
            """WITH dates AS (
                   SELECT DISTINCT trading_date FROM quant.canonical_bars_daily
                    WHERE quality_status IN ('fresh','partial')
               ), expected AS (
                   SELECT dates.trading_date,count(DISTINCT member.symbol)::int AS expected_symbols
                     FROM dates JOIN quant.universe_membership_history member
                       ON member.universe_key='all_a' AND member.effective_from<=dates.trading_date
                      AND (member.effective_to IS NULL OR member.effective_to>=dates.trading_date)
                    GROUP BY dates.trading_date
               ), covered AS (
                   SELECT bar.trading_date,count(DISTINCT bar.symbol)::int AS adjusted_symbols
                     FROM quant.canonical_bars_daily bar
                     JOIN quant.universe_membership_history member
                       ON member.universe_key='all_a' AND member.symbol=bar.symbol
                      AND member.effective_from<=bar.trading_date
                      AND (member.effective_to IS NULL OR member.effective_to>=bar.trading_date)
                    WHERE bar.quality_status IN ('fresh','partial') AND bar.adj_factor IS NOT NULL
                    GROUP BY bar.trading_date
               ) SELECT expected.trading_date
                     FROM expected JOIN covered USING(trading_date)
                    WHERE covered.adjusted_symbols>=%s
                      AND covered.adjusted_symbols>=ceil(expected.expected_symbols*0.95)::int
                    ORDER BY expected.trading_date DESC LIMIT 1""",
            (minimum_full_market_symbols,),
        ).fetchone()
    return row["trading_date"] if row else None


def load_ten_day_ranking_inputs(database: Any, as_of_date: date) -> TenDayRankingInputs:
    """Load an exact universe snapshot and at most eleven stored bars per symbol."""
    with database.transaction() as connection:
        coverage = connection.execute(
            """WITH active AS (
                   SELECT DISTINCT symbol FROM quant.universe_membership_history
                    WHERE universe_key='all_a' AND effective_from<=%s
                      AND (effective_to IS NULL OR effective_to>=%s)
               ) SELECT count(DISTINCT active.symbol)::int AS expected_daily_symbols,
                      count(DISTINCT bar.symbol) FILTER (WHERE bar.adj_factor IS NOT NULL)::int AS daily_symbols,
                      max(bar.available_at) AS strategy_available_at
                 FROM active LEFT JOIN quant.canonical_bars_daily bar
                   ON bar.symbol=active.symbol AND bar.trading_date=%s
                  AND bar.quality_status IN ('fresh','partial')""",
            (as_of_date, as_of_date, as_of_date),
        ).fetchone()
        rows = connection.execute(
            """WITH active AS (
                   SELECT DISTINCT symbol FROM quant.universe_membership_history
                    WHERE universe_key='all_a' AND effective_from<=%s
                      AND (effective_to IS NULL OR effective_to>=%s)
               ), ranked AS (
                   SELECT bar.symbol,instrument.name,bar.trading_date,bar.open,bar.high,bar.low,
                          bar.close,bar.pre_close,bar.volume,bar.amount,bar.adj_factor,
                          bar.is_suspended,bar.limit_up,bar.limit_down,bar.available_at,
                          row_number() OVER(PARTITION BY bar.symbol ORDER BY bar.trading_date DESC) rn
                     FROM quant.canonical_bars_daily bar JOIN active USING(symbol)
                     LEFT JOIN quant.instruments instrument ON instrument.symbol=bar.symbol
                    WHERE bar.trading_date<=%s AND bar.trading_date>=%s
                      AND bar.quality_status IN ('fresh','partial')
               ) SELECT symbol,name,trading_date,open,high,low,close,pre_close,volume,amount,
                        adj_factor,is_suspended,limit_up,limit_down,available_at
                   FROM ranked WHERE rn<=11 ORDER BY symbol,trading_date""",
            (as_of_date, as_of_date, as_of_date, as_of_date - timedelta(days=45)),
        ).fetchall()
    daily_rows = [dict(row) for row in rows]
    timestamps = [row.get("available_at") for row in daily_rows if row.get("available_at") is not None]
    coverage_available = coverage.get("strategy_available_at") if coverage else None
    if coverage_available is not None:
        timestamps.append(coverage_available)
    return TenDayRankingInputs(
        daily_rows=daily_rows,
        daily_symbols=int((coverage or {}).get("daily_symbols") or 0),
        strategy_available_at=max(timestamps, default=None),
        expected_daily_symbols=int((coverage or {}).get("expected_daily_symbols") or 0),
    )


def persist_ten_day_rotation_run(
    database: Any,
    *,
    run_key: str,
    as_of_date: date,
    strategy_available_at: datetime | None,
    model_version: str,
    status: str,
    source_status: dict[str, Any],
    summary: dict[str, Any],
    candidates: list[dict[str, Any]],
    json_safe: Callable[[Any], Any],
) -> Any:
    """Atomically replace one date/model projection without evaluating rules."""
    with database.transaction() as connection:
        run = connection.execute(
            """INSERT INTO quant.ten_day_leader_rotation_runs(
                       run_key,as_of_date,strategy_available_at,model_version,status,source_status,summary)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(run_key) DO UPDATE SET strategy_available_at=EXCLUDED.strategy_available_at,
                     status=EXCLUDED.status,source_status=EXCLUDED.source_status,summary=EXCLUDED.summary,
                     updated_at=now() RETURNING run_id""",
            (run_key, as_of_date, strategy_available_at, model_version, status,
             Json(json_safe(source_status)), Json(json_safe(summary))),
        ).fetchone()
        connection.execute(
            "DELETE FROM quant.ten_day_leader_rotation_candidates WHERE run_id=%s", (run["run_id"],),
        )
        for candidate in candidates:
            connection.execute(
                """INSERT INTO quant.ten_day_leader_rotation_candidates(
                           run_id,board,board_rank,symbol,name,ten_day_return_pct,current_return_pct,
                           candidate_path,shadow_state,shadow_eligible,decision_eligible,evidence,
                           reason_codes,risk_flags,source_snapshot)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run["run_id"], candidate["board"], candidate["ten_day_rank"], candidate["symbol"],
                 candidate.get("name"), candidate["ten_day_return_pct"], candidate["current_return_pct"],
                 candidate.get("candidate_path"), candidate["shadow_state"], candidate["shadow_eligible"],
                 candidate["decision_eligible"], Json(json_safe(candidate.get("evidence") or {})),
                 Json(candidate.get("reason_codes") or []), Json(candidate.get("risk_flags") or []),
                 Json(json_safe({"source_available_at": candidate.get("source_available_at")}))),
            )
    return run["run_id"]


def completed_for_date(database: Any, as_of_date: date, *, model_version: str) -> bool:
    """Check only the requested exchange date and methodology version."""
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT status FROM quant.ten_day_leader_rotation_runs
                WHERE as_of_date=%s AND model_version=%s ORDER BY updated_at DESC LIMIT 1""",
            (as_of_date, model_version),
        ).fetchone()
    return bool(row and row["status"] in {"completed", "partial"})


__all__ = [
    "TenDayRankingInputs", "completed_for_date", "latest_full_market_date",
    "load_ten_day_ranking_inputs", "persist_ten_day_rotation_run",
]
