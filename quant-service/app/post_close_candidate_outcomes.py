"""Outcome settlement for post-close/leader-rotation candidate lines.

``post_close_strategy_candidates`` and ``ten_day_leader_rotation_candidates``
are both post-close, run_id/symbol-keyed shadow candidate lines with no
existing outcome linkage: nothing ever measured what happened after either
strategy proposed a symbol, so neither could ever accumulate the evidence its
own promotion gate requires. Both tables are structurally identical for
settlement purposes, so one parametrized function settles either.

Every candidate is treated as a long/watch idea (neither table carries an
explicit direction). Entry is the next session's open after the run's
as_of_date, matching outcome_recomputation.py's convention; a locked
limit-up open or a suspended entry session is left unsettled rather than
credited with an unreachable fill.

The forward horizon is not documented by either upstream strategy. 10
trading days is used because it already is this codebase's dominant
convention for a comparable shadow evaluation window (HORIZON_DAYS in
watchlist_main_wave.py and watchlist_countertrend_rebound.py, and the
explicit ``return_horizon_sessions: 10`` ten_day_leader_ranking.py already
states for its own trailing measurement) - not a claim that either strategy
was designed around exactly 10 days.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, NamedTuple


class CandidateOutcomeTarget(NamedTuple):
    candidates_table: str
    runs_table: str
    outcomes_table: str
    horizon_days: int


POST_CLOSE_STRATEGY_CANDIDATES = CandidateOutcomeTarget(
    "post_close_strategy_candidates", "post_close_strategy_runs", "post_close_strategy_candidate_outcomes", 10,
)
TEN_DAY_LEADER_ROTATION_CANDIDATES = CandidateOutcomeTarget(
    "ten_day_leader_rotation_candidates", "ten_day_leader_rotation_runs", "ten_day_leader_rotation_candidate_outcomes", 10,
)


def settle_candidate_outcomes(connection: Any, as_of_date: date, target: CandidateOutcomeTarget) -> int:
    """Settle every candidate whose full forward window is already observable.

    ``target``'s table names are internal, hardcoded module constants, never
    request input, so building the query with an f-string is safe here.
    """
    rows = connection.execute(
        f"""WITH eligible AS (
                SELECT c.run_id,c.symbol,r.as_of_date run_date,
                  (SELECT b.trading_date FROM quant.canonical_bars_daily b
                   WHERE b.symbol=c.symbol AND b.trading_date>r.as_of_date AND b.trading_date<=%s
                   ORDER BY b.trading_date LIMIT 1) entry_date
                FROM quant.{target.candidates_table} c
                JOIN quant.{target.runs_table} r ON r.run_id=c.run_id
              ), priced AS (
                SELECT e.*, entry.open entry_price, entry.is_suspended entry_is_suspended, entry.limit_up entry_limit_up,
                  (SELECT close FROM quant.canonical_bars_daily b WHERE b.symbol=e.symbol AND b.trading_date>=e.entry_date
                   ORDER BY b.trading_date OFFSET %s LIMIT 1) exit_close,
                  benchmark_entry.close benchmark_entry_close,
                  (SELECT close FROM quant.canonical_bars_daily b WHERE b.symbol='000300.SH' AND b.trading_date>=e.entry_date
                   ORDER BY b.trading_date OFFSET %s LIMIT 1) benchmark_exit_close
                FROM eligible e
                JOIN quant.canonical_bars_daily entry ON entry.symbol=e.symbol AND entry.trading_date=e.entry_date
                LEFT JOIN quant.canonical_bars_daily benchmark_entry ON benchmark_entry.symbol='000300.SH' AND benchmark_entry.trading_date=e.entry_date
              )
              -- A locked limit-up open or a suspended entry session is not a
              -- fillable long entry; leave it unsettled rather than credit an
              -- unreachable fill (every candidate here is a long/watch idea).
              SELECT * FROM priced
              WHERE exit_close IS NOT NULL AND entry_price IS NOT NULL AND NOT entry_is_suspended
                AND (entry_limit_up IS NULL OR entry_price<entry_limit_up*0.999)""",
        (as_of_date, target.horizon_days - 1, target.horizon_days - 1),
    ).fetchall()
    settled = 0
    for row in rows:
        entry_price, exit_close = Decimal(row["entry_price"]), Decimal(row["exit_close"])
        raw_return = exit_close / entry_price - 1
        benchmark_return = (Decimal(row["benchmark_exit_close"]) / Decimal(row["benchmark_entry_close"]) - 1
                            if row["benchmark_exit_close"] and row["benchmark_entry_close"] else None)
        path = connection.execute(
            """SELECT high,low,close FROM quant.canonical_bars_daily
                 WHERE symbol=%s AND trading_date>=%s AND trading_date<=%s
                 ORDER BY trading_date""",
            (row["symbol"], row["entry_date"], connection.execute(
                 """SELECT trading_date FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date>=%s
                      ORDER BY trading_date OFFSET %s LIMIT 1""",
                 (row["symbol"], row["entry_date"], target.horizon_days - 1),
             ).fetchone()["trading_date"]),
        ).fetchall()
        highs = [Decimal(bar["high"] or bar["close"]) for bar in path]
        lows = [Decimal(bar["low"] or bar["close"]) for bar in path]
        mfe = max(highs) / entry_price - 1
        mae = min(lows) / entry_price - 1
        connection.execute(
            f"""INSERT INTO quant.{target.outcomes_table}(
                    run_id,symbol,entry_date,horizon_days,entry_price,exit_price,raw_return,benchmark_return,
                    excess_return,maximum_favorable_excursion,maximum_adverse_excursion,tradability)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'observed_open')
                ON CONFLICT(run_id,symbol) DO UPDATE SET exit_price=EXCLUDED.exit_price,raw_return=EXCLUDED.raw_return,
                  benchmark_return=EXCLUDED.benchmark_return,excess_return=EXCLUDED.excess_return,
                  maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
                  maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,tradability=EXCLUDED.tradability,
                  calculated_at=now()""",
            (row["run_id"], row["symbol"], row["entry_date"], target.horizon_days, row["entry_price"], row["exit_close"],
             raw_return, benchmark_return, raw_return - benchmark_return if benchmark_return is not None else None, mfe, mae),
        )
        settled += 1
    return settled


def settle_post_close_and_leader_rotation_outcomes(connection: Any, as_of_date: date) -> dict[str, int]:
    return {
        "post_close_strategy_candidate_outcomes": settle_candidate_outcomes(connection, as_of_date, POST_CLOSE_STRATEGY_CANDIDATES),
        "ten_day_leader_rotation_candidate_outcomes": settle_candidate_outcomes(connection, as_of_date, TEN_DAY_LEADER_ROTATION_CANDIDATES),
    }


__all__ = [
    "POST_CLOSE_STRATEGY_CANDIDATES", "TEN_DAY_LEADER_ROTATION_CANDIDATES", "CandidateOutcomeTarget",
    "settle_candidate_outcomes", "settle_post_close_and_leader_rotation_outcomes",
]
