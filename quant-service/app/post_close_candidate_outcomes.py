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
credited with an unreachable fill. The exit session is resolved from the
trade calendar and instrument lifecycle (outcome_recomputation.resolve_exit),
never from the symbol's own bar count: a delisted symbol settles at its last
observed close instead of vanishing from the sample, and a suspension gap
inside the window leaves the candidate unsettled instead of silently
stretching the holding period past it.

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

from .market_rules import LIMIT_TOLERANCE
from .outcome_recomputation import _archive_before_overwrite, bars_snapshot_hash, resolve_benchmark_close, resolve_exit

#: Bumped from ``post-close-candidate-outcome-v1`` (the version the migration
#: backfills onto pre-existing rows): the exit session is now resolved from
#: the trade calendar and instrument lifecycle instead of the candidate's own
#: bar count, so a delisted symbol settles at its last close instead of
#: never settling, and a suspension gap can no longer silently stretch the
#: holding period.
METHODOLOGY_VERSION = "post-close-candidate-outcome-v2"


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
    """Settle every candidate whose fillable entry is already observable.

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
              )
              -- A locked limit-up open or a suspended entry session is not a
              -- fillable long entry; leave it unsettled rather than credit an
              -- unreachable fill (every candidate here is a long/watch idea).
              -- The exit session is resolved afterwards from the trade
              -- calendar, not from this join.
              SELECT e.*, entry.open entry_price, entry.is_suspended entry_is_suspended, entry.limit_up entry_limit_up,
                benchmark_entry.close benchmark_entry_close
              FROM eligible e
              JOIN quant.canonical_bars_daily entry ON entry.symbol=e.symbol AND entry.trading_date=e.entry_date
              LEFT JOIN quant.canonical_bars_daily benchmark_entry ON benchmark_entry.symbol='000300.SH' AND benchmark_entry.trading_date=e.entry_date
              WHERE entry.open IS NOT NULL AND NOT entry.is_suspended
                AND (entry.limit_up IS NULL OR entry.open<entry.limit_up-{LIMIT_TOLERANCE})""",
        (as_of_date,),
    ).fetchall()
    settled = 0
    for row in rows:
        resolved = resolve_exit(connection, row["symbol"], row["entry_date"], target.horizon_days, as_of_date)
        if resolved["status"] in ("pending", "suspension_in_window"):
            continue
        entry_price = Decimal(row["entry_price"])
        exit_close = Decimal(resolved["exit_close"])
        raw_return = exit_close / entry_price - 1
        benchmark_exit_close = resolve_benchmark_close(connection, resolved["target_exit_date"])
        benchmark_return = (Decimal(benchmark_exit_close) / Decimal(row["benchmark_entry_close"]) - 1
                            if benchmark_exit_close is not None and row["benchmark_entry_close"] else None)
        path = connection.execute(
            """SELECT trading_date,high,low,close FROM quant.canonical_bars_daily
                 WHERE symbol=%s AND trading_date>=%s AND trading_date<=%s
                 ORDER BY trading_date""",
            (row["symbol"], row["entry_date"], resolved["exit_date"]),
        ).fetchall()
        highs = [Decimal(bar["high"] or bar["close"]) for bar in path]
        lows = [Decimal(bar["low"] or bar["close"]) for bar in path]
        mfe = max(highs) / entry_price - 1
        mae = min(lows) / entry_price - 1
        tradability = "delisted" if resolved["status"] == "delisted" else "observed_open"
        snapshot_hash = bars_snapshot_hash([
            (str(bar["trading_date"]), str(bar["high"]), str(bar["low"]), str(bar["close"])) for bar in path
        ])
        _archive_before_overwrite(
            connection, target.outcomes_table, "run_id=%s AND symbol=%s", (row["run_id"], row["symbol"]),
            ("run_id", "symbol"),
        )
        connection.execute(
            f"""INSERT INTO quant.{target.outcomes_table}(
                    run_id,symbol,entry_date,horizon_days,entry_price,exit_price,raw_return,benchmark_return,
                    excess_return,maximum_favorable_excursion,maximum_adverse_excursion,tradability,
                    methodology_version,bars_snapshot_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(run_id,symbol) DO UPDATE SET exit_price=EXCLUDED.exit_price,raw_return=EXCLUDED.raw_return,
                  benchmark_return=EXCLUDED.benchmark_return,excess_return=EXCLUDED.excess_return,
                  maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
                  maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,tradability=EXCLUDED.tradability,
                  methodology_version=EXCLUDED.methodology_version,bars_snapshot_hash=EXCLUDED.bars_snapshot_hash,
                  calculated_at=now()""",
            (row["run_id"], row["symbol"], row["entry_date"], target.horizon_days, row["entry_price"], resolved["exit_close"],
             raw_return, benchmark_return, raw_return - benchmark_return if benchmark_return is not None else None, mfe, mae,
             tradability, METHODOLOGY_VERSION, snapshot_hash),
        )
        settled += 1
    return settled


def settle_post_close_and_leader_rotation_outcomes(connection: Any, as_of_date: date) -> dict[str, int]:
    return {
        "post_close_strategy_candidate_outcomes": settle_candidate_outcomes(connection, as_of_date, POST_CLOSE_STRATEGY_CANDIDATES),
        "ten_day_leader_rotation_candidate_outcomes": settle_candidate_outcomes(connection, as_of_date, TEN_DAY_LEADER_ROTATION_CANDIDATES),
    }


__all__ = [
    "METHODOLOGY_VERSION", "POST_CLOSE_STRATEGY_CANDIDATES", "TEN_DAY_LEADER_ROTATION_CANDIDATES", "CandidateOutcomeTarget",
    "settle_candidate_outcomes", "settle_post_close_and_leader_rotation_outcomes",
]
