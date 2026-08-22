"""Read-only daily strategy summary projection, independent of HTTP wiring."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable


def build_daily_strategy_summary(database: Any, exchange_date: date, *, readiness: Callable[[Any], Any],
                                 json_safe: Callable[[Any], Any], policy_review: Callable[..., Any]) -> dict[str, Any]:
    with database.transaction() as connection:
        signal_rows = connection.execute(
            """SELECT state,count(*)::int AS count FROM quant.intraday_signal_events
                 WHERE observed_at AT TIME ZONE 'Asia/Shanghai' >= %s
                   AND observed_at AT TIME ZONE 'Asia/Shanghai' < %s GROUP BY state""",
            (exchange_date, exchange_date + timedelta(days=1)),
        ).fetchall()
        outcome_rows = connection.execute(
            """SELECT horizon_key,status,count(*)::int AS count FROM quant.intraday_signal_outcomes
                 WHERE entry_observed_at AT TIME ZONE 'Asia/Shanghai' >= %s
                   AND entry_observed_at AT TIME ZONE 'Asia/Shanghai' < %s GROUP BY horizon_key,status""",
            (exchange_date, exchange_date + timedelta(days=1)),
        ).fetchall()
        learning_rows = connection.execute(
            """SELECT s.signal_event_id,s.signal_type,s.observed_at,s.evidence,
                      (s.observed_at AT TIME ZONE 'Asia/Shanghai')::date AS exchange_date,
                      o.status,o.raw_return,o.maximum_favorable_excursion,o.maximum_adverse_excursion
                 FROM quant.intraday_signal_events s
                 LEFT JOIN quant.intraday_signal_outcomes o
                   ON o.signal_event_id=s.signal_event_id AND o.horizon_key='30m'
                WHERE s.state='alerted' AND s.signal_type IN ('entry','watch','reduce','exit')
                ORDER BY s.observed_at""",
        ).fetchall()
        post_close_run = connection.execute(
            """SELECT run_id,status,summary FROM quant.post_close_strategy_runs
                 WHERE as_of_date=%s ORDER BY updated_at DESC LIMIT 1""", (exchange_date,),
        ).fetchone()
        candidates = []
        if post_close_run:
            candidates = connection.execute(
                """SELECT c.symbol,i.name,c.candidate_type,c.score
                     FROM quant.post_close_strategy_candidates c LEFT JOIN quant.instruments i ON i.symbol=c.symbol
                    WHERE c.run_id=%s ORDER BY c.rank LIMIT 5""", (post_close_run["run_id"],),
            ).fetchall()
        close_review = connection.execute(
            """SELECT market_state,data_boundary FROM quant.strategy_review_runs
                 WHERE exchange_date=%s AND session='close' ORDER BY observed_at DESC LIMIT 1""", (exchange_date,),
        ).fetchone()
        readiness_result = readiness(connection)
    signal_counts = {str(row["state"]): int(row["count"] or 0) for row in signal_rows}
    outcome_counts: dict[str, dict[str, int]] = {}
    for row in outcome_rows:
        outcome_counts.setdefault(str(row["horizon_key"]), {})[str(row["status"])] = int(row["count"] or 0)
    learning_input = [{**json_safe(dict(row)), "exchange_date": str(exchange_date)} for row in learning_rows]
    return {
        "exchange_date": str(exchange_date), "signal_counts": signal_counts,
        "outcome_counts": outcome_counts,
        "post_close": {
            "status": post_close_run["status"] if post_close_run else "missing",
            "reason": ((post_close_run["summary"] or {}).get("reason") if post_close_run else "post-close strategy has not produced a run"),
            "candidates": [dict(row) for row in candidates],
        },
        "close_review": json_safe(dict(close_review)) if close_review else None,
        "readiness": readiness_result,
        "offline_policy_learning": policy_review(learning_input, focus_exchange_date=str(exchange_date)),
    }


def terminal_for_exchange_date(connection: Any, exchange_date: date) -> bool:
    """Whether the persisted day summary is a restart-safe terminal receipt."""
    row = connection.execute(
        """SELECT 1 FROM quant.strategy_day_summaries
             WHERE exchange_date=%s AND delivery_status=ANY(%s)
             LIMIT 1""",
        (exchange_date, ["sent", "disabled", "suppressed"]),
    ).fetchone()
    return row is not None


__all__ = ["build_daily_strategy_summary", "terminal_for_exchange_date"]
