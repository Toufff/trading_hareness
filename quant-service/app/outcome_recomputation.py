"""Local-only daily outcome recomputation for analyst claims and recommendations."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Callable

from .json_safe_encoding import json_safe
from .market_rules import LIMIT_TOLERANCE

#: Bumped from ``outcome-recomputation-v1`` (the version the migration backfills
#: onto pre-existing rows) because this version changed the computation, not
#: just added bookkeeping columns: the exit session is now resolved from the
#: trade calendar and instrument lifecycle (never "the symbol's Nth own bar",
#: which silently stretched a holding period across a suspension gap), a
#: delisted symbol settles at its last observed close instead of vanishing
#: from the sample, and a 1-day horizon can no longer exit the same session
#: it entered.
METHODOLOGY_VERSION = "outcome-recomputation-v2"


def _exit_offset(horizon_days: int) -> int:
    """Trading-session OFFSET from ``entry_date`` to the horizon's exit session.

    Equal to ``backtest_execution_rules.a_share_exit_lag(horizon_days - 1) - 1``
    for every horizon (see ``test_outcome_recomputation.py`` for the parity
    assertion actually calling ``a_share_exit_lag``): a signal entered at
    ``entry_date``'s open cannot exit at that same session's close under T+1,
    so a 1-day horizon's exit is pushed one session later than a naive
    ``horizon_days - 1`` offset would give.  Horizons of 2+ days are
    unaffected, because their close-of-day-N exit was already at least one
    full session after entry.
    """
    return max(1, int(horizon_days) - 1)


def resolve_exit(connection: Any, symbol: str, entry_date: Any, horizon_days: int, as_of_date: Any) -> dict[str, Any]:
    """Resolve one horizon's exit session from the trade calendar and instrument lifecycle.

    Never falls back to "the symbol's Nth own bar after entry": that
    quietly stretches the realized holding period across a suspension gap
    and makes a same-calendar-day benchmark comparison meaningless. A
    calendar session with no matching bar for a still-listed symbol is left
    ``suspension_in_window`` (unsettled) rather than guessed at; a delisted
    symbol settles at its last observed close so it stays in the sample
    instead of disappearing from the denominator.
    """
    if entry_date is None:
        return {"status": "pending"}
    target = connection.execute(
        """SELECT calendar_date FROM quant.market_trade_calendar
             WHERE exchange='SSE' AND is_open AND calendar_date>=%s AND calendar_date<=%s
             ORDER BY calendar_date OFFSET %s LIMIT 1""",
        (entry_date, as_of_date, _exit_offset(horizon_days)),
    ).fetchone()
    if target is None:
        return {"status": "pending"}
    target_exit_date = target["calendar_date"]
    instrument = connection.execute(
        "SELECT delist_date FROM quant.instruments WHERE symbol=%s", (symbol,),
    ).fetchone()
    delist_date = instrument["delist_date"] if instrument else None
    if delist_date is not None and delist_date <= target_exit_date:
        last_bar = connection.execute(
            """SELECT trading_date,close FROM quant.canonical_bars_daily
                 WHERE symbol=%s AND trading_date<=%s ORDER BY trading_date DESC LIMIT 1""",
            (symbol, delist_date),
        ).fetchone()
        if last_bar is None:
            return {"status": "pending", "target_exit_date": target_exit_date}
        return {"status": "delisted", "exit_date": last_bar["trading_date"], "exit_close": last_bar["close"],
                "target_exit_date": target_exit_date}
    exit_bar = connection.execute(
        "SELECT trading_date,close FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date=%s",
        (symbol, target_exit_date),
    ).fetchone()
    if exit_bar is None:
        return {"status": "suspension_in_window", "target_exit_date": target_exit_date}
    return {"status": "settled", "exit_date": exit_bar["trading_date"], "exit_close": exit_bar["close"],
            "target_exit_date": target_exit_date}


def resolve_benchmark_close(connection: Any, target_exit_date: Any, benchmark_symbol: str = "000300.SH") -> Any:
    """The benchmark close on the same calendar exit date, never the benchmark's own OFFSET bar."""
    row = connection.execute(
        "SELECT close FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date=%s",
        (benchmark_symbol, target_exit_date),
    ).fetchone()
    return row["close"] if row else None


def bars_snapshot_hash(*parts: Any) -> str:
    """A reproducibility fingerprint of the bars an outcome was computed from.

    Two recomputations that land on the same primary key but were fed
    different underlying bars (a provider correction, a unit fix) produce a
    different hash even though the outcome row's identity did not change,
    which is otherwise invisible once the row is overwritten.
    """
    return hashlib.sha256(json.dumps(json_safe(parts), sort_keys=True, default=str).encode()).hexdigest()


def _archive_before_overwrite(connection: Any, table: str, predicate: str, params: tuple[Any, ...],
                              key_columns: tuple[str, ...]) -> None:
    """Copy the row about to be overwritten (if any) into ``<table>_history``.

    The live table keeps holding only the latest computation (so no reader
    of it has to change), while every value it ever superseded remains
    queryable for reproducibility review. A no-op (0 rows inserted) on a
    fresh insert, since the ``SELECT`` side matches nothing yet.
    """
    key_list = ", ".join(key_columns)
    prefix = f"{key_list}, " if key_columns else ""
    connection.execute(
        f"""INSERT INTO quant.{table}_history({prefix}methodology_version, bars_snapshot_hash, old_row)
             SELECT {prefix}methodology_version, bars_snapshot_hash, to_jsonb(t)
               FROM quant.{table} t WHERE {predicate}""",
        params,
    )


def recompute(
    as_of_date: Any = None,
    *,
    cn_today: Callable[[], Any],
    db: Any,
    recompute_intraday_signal_outcomes: Callable[[Any], dict[str, Any]],
    settle_post_close_and_leader_rotation_outcomes: Callable[[Any, Any], dict[str, int]] | None = None,
    settle_ledger_outcomes: Callable[[Any, Any], int] | None = None,
) -> dict[str, Any]:
    """Close only outcomes whose already-persisted future bars are observable."""
    as_of_date = as_of_date or cn_today()
    with db.transaction() as connection:
        rows = connection.execute(
            r"""WITH eligible AS (
                SELECT c.claim_id,c.subject_key symbol,c.horizon_days,c.direction,
                  (SELECT b.trading_date FROM quant.canonical_bars_daily b
                   WHERE b.symbol=c.subject_key
                     AND b.trading_date>(c.available_at AT TIME ZONE 'Asia/Shanghai')::date
                     AND b.trading_date<=%s
                   ORDER BY b.trading_date LIMIT 1) entry_date
                FROM quant.analyst_claims c
                WHERE c.scope='stock' AND c.subject_key ~ '^\d{6}\.(SH|SZ|BJ)$' AND c.direction<>0
              )
              -- Entry is next-session open; a locked limit-up (long) or limit-down (short-thesis)
              -- open, or a suspended session, is not a fillable entry and is left unsettled
              -- rather than credited/debited as if a real order could have been placed.  The
              -- exit session is resolved afterwards from the trade calendar, not from this join.
              SELECT e.*, entry.open entry_price, entry.is_suspended entry_is_suspended,
                entry.limit_up entry_limit_up, entry.limit_down entry_limit_down,
                benchmark_entry.close benchmark_entry_close
              FROM eligible e
              JOIN quant.canonical_bars_daily entry ON entry.symbol=e.symbol AND entry.trading_date=e.entry_date
              LEFT JOIN quant.canonical_bars_daily benchmark_entry ON benchmark_entry.symbol='000300.SH' AND benchmark_entry.trading_date=e.entry_date
              WHERE entry.open IS NOT NULL AND NOT entry.is_suspended
                AND ((e.direction>0 AND (entry.limit_up IS NULL OR entry.open<entry.limit_up-%s))
                  OR (e.direction<0 AND (entry.limit_down IS NULL OR entry.open>entry.limit_down+%s)))""",
            (as_of_date, LIMIT_TOLERANCE, LIMIT_TOLERANCE),
        ).fetchall()
        settled = 0
        for row in rows:
            direction = int(row["direction"])
            resolved = resolve_exit(connection, row["symbol"], row["entry_date"], int(row["horizon_days"]), as_of_date)
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
            maximum_favorable_excursion = (max(highs) / entry_price - 1) if direction > 0 else (entry_price / min(lows) - 1)
            maximum_adverse_excursion = (min(lows) / entry_price - 1) if direction > 0 else (entry_price / max(highs) - 1)
            tradability = "delisted" if resolved["status"] == "delisted" else "observed_open"
            snapshot_hash = bars_snapshot_hash([
                (str(bar["trading_date"]), str(bar["high"]), str(bar["low"]), str(bar["close"])) for bar in path
            ])
            _archive_before_overwrite(
                connection, "outcomes", "claim_id=%s AND symbol=%s AND entry_date=%s AND horizon_days=%s",
                (row["claim_id"], row["symbol"], row["entry_date"], row["horizon_days"]),
                ("claim_id", "symbol", "entry_date", "horizon_days"),
            )
            connection.execute(
                """INSERT INTO quant.outcomes(claim_id,symbol,entry_date,horizon_days,entry_close,exit_close,raw_return,benchmark_return,excess_return,
                      maximum_favorable_excursion,maximum_adverse_excursion,tradability,direction,methodology_version,bars_snapshot_hash)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(claim_id,symbol,entry_date,horizon_days) DO UPDATE SET exit_close=EXCLUDED.exit_close,raw_return=EXCLUDED.raw_return,
                     benchmark_return=EXCLUDED.benchmark_return,excess_return=EXCLUDED.excess_return,
                     maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
                     maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,direction=EXCLUDED.direction,tradability=EXCLUDED.tradability,
                     methodology_version=EXCLUDED.methodology_version,bars_snapshot_hash=EXCLUDED.bars_snapshot_hash,calculated_at=now()""",
                (row["claim_id"], row["symbol"], row["entry_date"], row["horizon_days"], row["entry_price"], resolved["exit_close"],
                 raw_return, benchmark_return, raw_return - benchmark_return if benchmark_return is not None else None,
                 maximum_favorable_excursion, maximum_adverse_excursion, tradability, direction, METHODOLOGY_VERSION, snapshot_hash),
            )
            settled += 1
        recommendation_rows = connection.execute(
            """SELECT r.run_id,r.as_of_date run_date,x.symbol,x.direction,x.horizon_days
               FROM quant.recommendation_runs r JOIN quant.recommendations x ON x.run_id=r.run_id
               WHERE r.as_of_date<=%s AND x.direction<>0""", (as_of_date,),
        ).fetchall()
        recommendation_outcomes = 0
        for recommendation in recommendation_rows:
            entry = connection.execute(
                """SELECT trading_date,open,is_suspended,limit_up,limit_down FROM quant.canonical_bars_daily
                     WHERE symbol=%s AND trading_date>%s AND trading_date<=%s
                     ORDER BY trading_date LIMIT 1""",
                (recommendation["symbol"], recommendation["run_date"], as_of_date),
            ).fetchone()
            if entry is None:
                continue
            direction = int(recommendation["direction"])
            # A locked limit-up (long) / limit-down (short-thesis) open, or a suspended
            # session, is not a fillable next-session entry; leave the run unsettled
            # rather than crediting/debiting an unreachable price.
            if entry["is_suspended"]:
                continue
            entry_limit_up, entry_limit_down = entry["limit_up"], entry["limit_down"]
            entry_open = Decimal(entry["open"])
            if direction > 0 and entry_limit_up is not None and entry_open >= Decimal(entry_limit_up) - Decimal(str(LIMIT_TOLERANCE)):
                continue
            if direction < 0 and entry_limit_down is not None and entry_open <= Decimal(entry_limit_down) + Decimal(str(LIMIT_TOLERANCE)):
                continue
            resolved = resolve_exit(connection, recommendation["symbol"], entry["trading_date"],
                                    int(recommendation["horizon_days"]), as_of_date)
            if resolved["status"] in ("pending", "suspension_in_window"):
                continue
            entry_price, exit_close = entry_open, Decimal(resolved["exit_close"])
            raw_return = (exit_close / entry_price - 1) * direction
            benchmark_entry = connection.execute(
                "SELECT close FROM quant.canonical_bars_daily WHERE symbol='000300.SH' AND trading_date=%s",
                (entry["trading_date"],),
            ).fetchone()
            benchmark_exit_close = resolve_benchmark_close(connection, resolved["target_exit_date"])
            benchmark_return = None
            if benchmark_entry and benchmark_exit_close is not None:
                benchmark_return = (Decimal(benchmark_exit_close) / Decimal(benchmark_entry["close"]) - 1) * direction
            bars = connection.execute(
                """SELECT trading_date,high,low,close FROM quant.canonical_bars_daily
                     WHERE symbol=%s AND trading_date>=%s AND trading_date<=%s
                     ORDER BY trading_date""",
                (recommendation["symbol"], entry["trading_date"], resolved["exit_date"]),
            ).fetchall()
            highs = [Decimal(bar["high"] or bar["close"]) for bar in bars]
            lows = [Decimal(bar["low"] or bar["close"]) for bar in bars]
            mfe = (max(highs) / entry_price - 1) if direction > 0 else (entry_price / min(lows) - 1)
            mae = (min(lows) / entry_price - 1) if direction > 0 else (entry_price / max(highs) - 1)
            tradability = "delisted" if resolved["status"] == "delisted" else "observed_open"
            snapshot_hash = bars_snapshot_hash([
                (str(bar["trading_date"]), str(bar["high"]), str(bar["low"]), str(bar["close"])) for bar in bars
            ])
            _archive_before_overwrite(
                connection, "outcomes", "recommendation_run_id=%s AND symbol=%s AND entry_date=%s AND horizon_days=%s",
                (recommendation["run_id"], recommendation["symbol"], entry["trading_date"], recommendation["horizon_days"]),
                ("recommendation_run_id", "symbol", "entry_date", "horizon_days"),
            )
            connection.execute(
                """INSERT INTO quant.outcomes(recommendation_run_id,symbol,entry_date,horizon_days,entry_close,exit_close,raw_return,
                      benchmark_return,excess_return,maximum_favorable_excursion,maximum_adverse_excursion,tradability,direction,
                      methodology_version,bars_snapshot_hash)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (recommendation_run_id,symbol,entry_date,horizon_days) WHERE recommendation_run_id IS NOT NULL
                   DO UPDATE SET exit_close=EXCLUDED.exit_close,raw_return=EXCLUDED.raw_return,benchmark_return=EXCLUDED.benchmark_return,
                     excess_return=EXCLUDED.excess_return,maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
                     maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,tradability=EXCLUDED.tradability,
                     methodology_version=EXCLUDED.methodology_version,bars_snapshot_hash=EXCLUDED.bars_snapshot_hash,calculated_at=now()""",
                (recommendation["run_id"], recommendation["symbol"], entry["trading_date"], recommendation["horizon_days"], entry_price,
                 exit_close, raw_return, benchmark_return, raw_return - benchmark_return if benchmark_return is not None else None,
                 mfe, mae, tradability, direction, METHODOLOGY_VERSION, snapshot_hash),
            )
            recommendation_outcomes += 1
        candidate_outcomes = (
            settle_post_close_and_leader_rotation_outcomes(connection, as_of_date)
            if settle_post_close_and_leader_rotation_outcomes is not None else {}
        )
        ledger_outcome_rows = settle_ledger_outcomes(connection, as_of_date) if settle_ledger_outcomes is not None else 0
    intraday = recompute_intraday_signal_outcomes(as_of_date)
    candidate_outcome_rows = sum(candidate_outcomes.values())
    return {"as_of_date": str(as_of_date),
            "outcomes": settled + recommendation_outcomes + intraday["outcome_rows"] + candidate_outcome_rows + ledger_outcome_rows,
            "claim_outcomes": settled, "recommendation_outcomes": recommendation_outcomes,
            "intraday_signal_outcomes": intraday, "candidate_outcomes": candidate_outcomes,
            "ledger_outcomes": ledger_outcome_rows, "methodology_version": METHODOLOGY_VERSION}


__all__ = [
    "METHODOLOGY_VERSION", "bars_snapshot_hash", "recompute", "resolve_benchmark_close", "resolve_exit",
]
