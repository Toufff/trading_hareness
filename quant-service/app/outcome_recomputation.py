"""Local-only daily outcome recomputation for analyst claims and recommendations."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable


def recompute(
    as_of_date: Any = None,
    *,
    cn_today: Callable[[], Any],
    db: Any,
    recompute_intraday_signal_outcomes: Callable[[Any], dict[str, Any]],
    settle_post_close_and_leader_rotation_outcomes: Callable[[Any, Any], dict[str, int]] | None = None,
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
              ), priced AS (
                SELECT e.*, entry.open entry_price, entry.is_suspended entry_is_suspended,
                  entry.limit_up entry_limit_up, entry.limit_down entry_limit_down,
                  (SELECT close FROM quant.canonical_bars_daily b WHERE b.symbol=e.symbol AND b.trading_date>=e.entry_date
                   ORDER BY b.trading_date OFFSET (e.horizon_days-1) LIMIT 1) exit_close,
                  benchmark_entry.close benchmark_entry_close,
                  (SELECT close FROM quant.canonical_bars_daily b WHERE b.symbol='000300.SH' AND b.trading_date>=e.entry_date
                   ORDER BY b.trading_date OFFSET (e.horizon_days-1) LIMIT 1) benchmark_exit_close
                FROM eligible e
                JOIN quant.canonical_bars_daily entry ON entry.symbol=e.symbol AND entry.trading_date=e.entry_date
                LEFT JOIN quant.canonical_bars_daily benchmark_entry ON benchmark_entry.symbol='000300.SH' AND benchmark_entry.trading_date=e.entry_date
              )
              -- Entry is next-session open; a locked limit-up (long) or limit-down (short-thesis)
              -- open, or a suspended session, is not a fillable entry and is left unsettled
              -- rather than credited/debited as if a real order could have been placed.
              SELECT * FROM priced
              WHERE exit_close IS NOT NULL AND entry_price IS NOT NULL
                AND NOT entry_is_suspended
                AND ((direction>0 AND (entry_limit_up IS NULL OR entry_price<entry_limit_up*0.999))
                  OR (direction<0 AND (entry_limit_down IS NULL OR entry_price>entry_limit_down*1.001)))""",
            (as_of_date,),
        ).fetchall()
        for row in rows:
            direction = int(row["direction"])
            raw_return = Decimal(row["exit_close"]) / Decimal(row["entry_price"]) - 1
            benchmark_return = (Decimal(row["benchmark_exit_close"]) / Decimal(row["benchmark_entry_close"]) - 1
                                if row["benchmark_exit_close"] and row["benchmark_entry_close"] else None)
            path = connection.execute(
                """SELECT high,low,close FROM quant.canonical_bars_daily
                     WHERE symbol=%s AND trading_date>=%s AND trading_date<=%s
                     ORDER BY trading_date""",
                (row["symbol"], row["entry_date"], connection.execute(
                     """SELECT trading_date FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date>=%s
                          ORDER BY trading_date OFFSET %s LIMIT 1""",
                     (row["symbol"], row["entry_date"], int(row["horizon_days"]) - 1),
                 ).fetchone()["trading_date"]),
            ).fetchall()
            highs = [Decimal(bar["high"] or bar["close"]) for bar in path]
            lows = [Decimal(bar["low"] or bar["close"]) for bar in path]
            entry_price = Decimal(row["entry_price"])
            maximum_favorable_excursion = (max(highs) / entry_price - 1) if direction > 0 else (entry_price / min(lows) - 1)
            maximum_adverse_excursion = (min(lows) / entry_price - 1) if direction > 0 else (entry_price / max(highs) - 1)
            connection.execute(
                """INSERT INTO quant.outcomes(claim_id,symbol,entry_date,horizon_days,entry_close,exit_close,raw_return,benchmark_return,excess_return,
                      maximum_favorable_excursion,maximum_adverse_excursion,tradability,direction)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'observed_open',%s)
                   ON CONFLICT(claim_id,symbol,entry_date,horizon_days) DO UPDATE SET exit_close=EXCLUDED.exit_close,raw_return=EXCLUDED.raw_return,
                     benchmark_return=EXCLUDED.benchmark_return,excess_return=EXCLUDED.excess_return,
                     maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
                     maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,direction=EXCLUDED.direction,tradability=EXCLUDED.tradability,calculated_at=now()""",
                (row["claim_id"], row["symbol"], row["entry_date"], row["horizon_days"], row["entry_price"], row["exit_close"],
                 raw_return, benchmark_return, raw_return - benchmark_return if benchmark_return is not None else None,
                 maximum_favorable_excursion, maximum_adverse_excursion, direction),
            )
        recommendation_rows = connection.execute(
            """SELECT r.run_id,r.as_of_date run_date,x.symbol,x.direction,x.horizon_days
               FROM quant.recommendation_runs r JOIN quant.recommendations x ON x.run_id=r.run_id
               WHERE r.as_of_date<=%s AND x.direction<>0""", (as_of_date,),
        ).fetchall()
        recommendation_outcomes = 0
        for recommendation in recommendation_rows:
            bars = connection.execute(
                """SELECT trading_date,close,high,low,open,is_suspended,limit_up,limit_down FROM quant.canonical_bars_daily
                   WHERE symbol=%s AND trading_date>%s AND trading_date<=%s
                   ORDER BY trading_date LIMIT %s""",
                (recommendation["symbol"], recommendation["run_date"], as_of_date, recommendation["horizon_days"]),
            ).fetchall()
            if len(bars) < int(recommendation["horizon_days"]):
                continue
            entry, exit_bar = bars[0], bars[-1]
            direction = int(recommendation["direction"])
            # A locked limit-up (long) / limit-down (short-thesis) open, or a suspended
            # session, is not a fillable next-session entry; leave the run unsettled
            # rather than crediting/debiting an unreachable price.
            if entry["is_suspended"]:
                continue
            entry_limit_up, entry_limit_down = entry["limit_up"], entry["limit_down"]
            entry_open = Decimal(entry["open"])
            if direction > 0 and entry_limit_up is not None and entry_open >= Decimal(entry_limit_up) * Decimal("0.999"):
                continue
            if direction < 0 and entry_limit_down is not None and entry_open <= Decimal(entry_limit_down) * Decimal("1.001"):
                continue
            entry_price, exit_close = entry_open, Decimal(exit_bar["close"])
            raw_return = (exit_close / entry_price - 1) * direction
            benchmark = connection.execute(
                """SELECT close FROM quant.canonical_bars_daily WHERE symbol='000300.SH' AND trading_date>=%s
                   ORDER BY trading_date LIMIT %s""",
                (entry["trading_date"], recommendation["horizon_days"]),
            ).fetchall()
            benchmark_return = None
            if len(benchmark) == int(recommendation["horizon_days"]):
                benchmark_return = (Decimal(benchmark[-1]["close"]) / Decimal(benchmark[0]["close"]) - 1) * direction
            highs = [Decimal(bar["high"] or bar["close"]) for bar in bars]
            lows = [Decimal(bar["low"] or bar["close"]) for bar in bars]
            mfe = (max(highs) / entry_price - 1) if direction > 0 else (entry_price / min(lows) - 1)
            mae = (min(lows) / entry_price - 1) if direction > 0 else (entry_price / max(highs) - 1)
            connection.execute(
                """INSERT INTO quant.outcomes(recommendation_run_id,symbol,entry_date,horizon_days,entry_close,exit_close,raw_return,
                      benchmark_return,excess_return,maximum_favorable_excursion,maximum_adverse_excursion,tradability,direction)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'observed_open',%s)
                   ON CONFLICT (recommendation_run_id,symbol,entry_date,horizon_days) WHERE recommendation_run_id IS NOT NULL
                   DO UPDATE SET exit_close=EXCLUDED.exit_close,raw_return=EXCLUDED.raw_return,benchmark_return=EXCLUDED.benchmark_return,
                     excess_return=EXCLUDED.excess_return,maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
                     maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,tradability=EXCLUDED.tradability,calculated_at=now()""",
                (recommendation["run_id"], recommendation["symbol"], entry["trading_date"], recommendation["horizon_days"], entry_price,
                 exit_close, raw_return, benchmark_return, raw_return - benchmark_return if benchmark_return is not None else None,
                 mfe, mae, direction),
            )
            recommendation_outcomes += 1
        candidate_outcomes = (
            settle_post_close_and_leader_rotation_outcomes(connection, as_of_date)
            if settle_post_close_and_leader_rotation_outcomes is not None else {}
        )
    intraday = recompute_intraday_signal_outcomes(as_of_date)
    candidate_outcome_rows = sum(candidate_outcomes.values())
    return {"as_of_date": str(as_of_date),
            "outcomes": len(rows) + recommendation_outcomes + intraday["outcome_rows"] + candidate_outcome_rows,
            "claim_outcomes": len(rows), "recommendation_outcomes": recommendation_outcomes,
            "intraday_signal_outcomes": intraday, "candidate_outcomes": candidate_outcomes}


__all__ = ["recompute"]
