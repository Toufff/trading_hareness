"""Settle xiaojie leader-flow observations against what actually happened.

Accumulating observations is only worth doing if each one eventually gets an
outcome attached. This computes them from the canonical daily bars once a
session closes, so a week of running produces an evaluable record rather than
a pile of entry snapshots.

Three returns are recorded because they answer different questions and the
first live session showed they disagree sharply:

``session_return_pct``        entry price to that session's close - what the
                              flag was worth on the day it fired.
``entry_to_next_close_pct``   the continuation question.
``next_open_to_close_pct``    the only one an account could actually have
                              earned, since an entry at the flagged price is
                              usually unavailable - a name flagged while locked
                              at the limit cannot be bought at all.

``sealed_at_entry`` is carried as its own column because it decides whether a
row is evaluable. On 2026-08-27, of 111 observations the 63 flagged while
already sealed produced 0 gains, 58 unchanged and 5 losses; the 48 flagged
unsealed averaged +0.78%. Averaging the two together halves every mode's
apparent edge and hides the real defect, which is flagging too late rather
than choosing wrongly.

The benchmark is the same session's cross-sectional median return, so a mode
is credited with what it added rather than with the market it rode.
"""

from __future__ import annotations

from datetime import date
from typing import Any


#: A session's close is only comparable against entries made during it.
SEALED_TOLERANCE = 0.005


def settle_session(connection: Any, trading_date: date) -> dict[str, Any]:
    """Attach outcomes to every observation of one session.

    Re-running is safe and refreshes: the next session's bars may not exist
    when this first runs at the close, so the forward columns fill in on a
    later pass rather than being frozen as null.
    """
    benchmark = connection.execute(
        """SELECT percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY (close / nullif(pre_close, 0) - 1) * 100) AS median_pct
             FROM quant.canonical_bars_daily b
             JOIN quant.instruments i ON i.symbol = b.symbol
            WHERE b.trading_date = %s AND b.volume > 0 AND i.list_date IS NOT NULL""",
        (trading_date,),
    ).fetchone()
    benchmark_pct = float(benchmark["median_pct"]) if benchmark and benchmark["median_pct"] is not None else None

    rows = connection.execute(
        """WITH observation AS (
             SELECT o.trading_date, o.symbol, o.mode, o.model_version, o.first_seen_at,
                    (o.alerted_at IS NOT NULL) AS alerted,
                    coalesce((o.first_evidence->'board'->>'sealed')::boolean, false) AS sealed_at_entry,
                    (o.first_evidence->>'price')::numeric AS entry_price
               FROM quant.xiaojie_leader_flow_observations o
              WHERE o.trading_date = %s AND o.first_evidence->>'price' IS NOT NULL
           ), session_bar AS (
             SELECT symbol, close, limit_up FROM quant.canonical_bars_daily
              WHERE trading_date = %s AND volume > 0
           ), next_bar AS (
             SELECT b.symbol, b.open, b.close, b.limit_up
               FROM quant.canonical_bars_daily b
               JOIN (SELECT min(trading_date) AS d FROM quant.canonical_bars_daily
                      WHERE trading_date > %s AND volume > 0) n ON n.d = b.trading_date
              WHERE b.volume > 0
           )
           SELECT observation.*, session_bar.close AS session_close,
                  next_bar.open AS next_open, next_bar.close AS next_close,
                  (next_bar.open >= next_bar.limit_up - %s) AS next_open_locked
             FROM observation
             LEFT JOIN session_bar USING (symbol)
             LEFT JOIN next_bar USING (symbol)""",
        (trading_date, trading_date, trading_date, SEALED_TOLERANCE),
    ).fetchall()

    settled = 0
    for row in rows:
        entry = float(row["entry_price"])
        if entry <= 0:
            continue

        def pct(target: Any, base: float = entry) -> float | None:
            return (float(target) / base - 1) * 100 if target is not None and base else None

        session_return = pct(row["session_close"])
        excess = (session_return - benchmark_pct
                  if session_return is not None and benchmark_pct is not None else None)
        next_open_to_close = (pct(row["next_close"], float(row["next_open"]))
                              if row["next_open"] and float(row["next_open"]) > 0 else None)
        connection.execute(
            """INSERT INTO quant.xiaojie_leader_flow_outcomes(
                    trading_date,symbol,mode,model_version,first_seen_at,alerted,sealed_at_entry,
                    entry_price,session_close,session_return_pct,next_open,next_close,
                    next_open_locked,entry_to_next_close_pct,next_open_to_close_pct,
                    benchmark_session_pct,excess_session_pct)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(trading_date,symbol,mode) DO UPDATE SET
                 session_close=EXCLUDED.session_close,
                 session_return_pct=EXCLUDED.session_return_pct,
                 next_open=EXCLUDED.next_open,next_close=EXCLUDED.next_close,
                 next_open_locked=EXCLUDED.next_open_locked,
                 entry_to_next_close_pct=EXCLUDED.entry_to_next_close_pct,
                 next_open_to_close_pct=EXCLUDED.next_open_to_close_pct,
                 benchmark_session_pct=EXCLUDED.benchmark_session_pct,
                 excess_session_pct=EXCLUDED.excess_session_pct,
                 alerted=EXCLUDED.alerted, settled_at=now()""",
            (row["trading_date"], row["symbol"], row["mode"], row["model_version"],
             row["first_seen_at"], row["alerted"], row["sealed_at_entry"], entry,
             row["session_close"], session_return, row["next_open"], row["next_close"],
             row["next_open_locked"], pct(row["next_close"]), next_open_to_close,
             benchmark_pct, excess),
        )
        settled += 1
    return {"trading_date": str(trading_date), "settled": settled,
            "benchmark_session_pct": benchmark_pct,
            "pending_next_session": sum(1 for row in rows if row["next_close"] is None)}


def mode_scorecard(connection: Any, start_date: date, end_date: date,
                   *, exclude_sealed: bool = True) -> list[dict[str, Any]]:
    """Aggregate settled outcomes per mode over a window.

    Sealed-at-entry rows are excluded by default: they are not evaluable as
    entries, and including them drags every mode toward zero for a reason that
    has nothing to do with whether the mode picks well.
    """
    rows = connection.execute(
        """SELECT mode,
                  count(*) AS observations,
                  count(*) FILTER (WHERE alerted) AS alerted,
                  round(avg(session_return_pct)::numeric, 4) AS avg_session_pct,
                  round(avg(excess_session_pct)::numeric, 4) AS avg_excess_pct,
                  round((100.0 * count(*) FILTER (WHERE session_return_pct > 0)
                         / nullif(count(*) FILTER (WHERE session_return_pct IS NOT NULL), 0))::numeric, 2)
                    AS session_win_pct,
                  round(avg(next_open_to_close_pct)::numeric, 4) AS avg_next_open_to_close_pct,
                  count(*) FILTER (WHERE next_open_locked) AS next_open_locked
             FROM quant.xiaojie_leader_flow_outcomes
            WHERE trading_date BETWEEN %s AND %s
              AND (NOT %s OR NOT sealed_at_entry)
            GROUP BY mode ORDER BY observations DESC""",
        (start_date, end_date, exclude_sealed),
    ).fetchall()
    return [dict(row) for row in rows]


__all__ = ["SEALED_TOLERANCE", "mode_scorecard", "settle_session"]
